import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .agent import Agent
from .gitops import complete, prepare
from .providers import model_config
from .events import command_context
from .workspaces import repo_config, repo_lock, worktree
from .logs import safe_log_dir
from .review import review_tree
from .gitops import git


DATA = Path(__file__).parent


def templates():
    return {p.stem: yaml.safe_load(p.read_text()) for p in (DATA / "templates").glob("*.yaml")}


def load_config(path):
    config = yaml.safe_load(path.read_text())
    for repo in config["repos"]:
        repo["path"] = str((path.parent / Path(repo["path"]).expanduser()).resolve())
    return config


def gh(root, *args):
    return subprocess.run(["gh", *args], cwd=root, check=True,
                          capture_output=True, text=True, timeout=120).stdout


def run(config, template_name, repo_name, params=None, dry_run=False, log_dir=Path("logs")):
    log_dir = safe_log_dir(config, log_dir)
    repo = next(r for r in config["repos"] if r["name"] == repo_name)
    root = Path(repo["path"]).resolve()
    with repo_lock(root):
        return _run(config, template_name, repo_name, params, dry_run, log_dir)


def isolated_run(config, template_name, repo_name, params=None, dry_run=False, log_dir=Path("logs")):
    log_dir = safe_log_dir(config, log_dir)
    repo = next(r for r in config["repos"] if r["name"] == repo_name)
    root = Path(repo["path"]).resolve()
    with repo_lock(root):
        path = worktree(root, repo["base_branch"])
        return _run(repo_config(config, repo_name, path), template_name, repo_name,
                    params, dry_run, log_dir)


def _run(config, template_name, repo_name, params=None, dry_run=False, log_dir=Path("logs")):
    log_dir = safe_log_dir(config, log_dir)
    template = templates()[template_name]
    repo = next(r for r in config["repos"] if r["name"] == repo_name)
    root = Path(repo["path"]).resolve()
    params = dict(params or {})
    values = {"task": "", "test_command": "python -m pytest -q",
              "lint_command": "ruff check . && mypy .", "context": "", **params}
    model = model_config(config["model"])
    api_key = os.environ.get(model["api_key_env"])
    if not api_key:
        raise ValueError("Missing API key environment variable")
    if template_name == "custom" and not values["task"].strip():
        raise ValueError("custom requires --task or automation task")
    log_dir.mkdir(parents=True, exist_ok=True)
    trigger = template.get("trigger", {}).get("type", "manual")
    start_ref = repo.get("start_ref", repo["base_branch"])
    publish_base = repo["base_branch"]
    if trigger == "command":
        contexts = command_context(root, template, params, log_dir)
    elif template_name == "fix_github_issues" or trigger == "github_issues":
        if not repo.get("github"):
            raise ValueError("fix_github_issues requires repo.github and gh")
        filters = ["--label", "bug"] if template_name == "fix_github_issues" else []
        issue_limit = int(params.get("issue_limit", config.get("issue_limit", 200)))
        if issue_limit < 1:
            raise ValueError("issue_limit must be positive")
        issues = json.loads(gh(root, "issue", "list", "--repo", repo["github"],
                               "--state", "open", *filters, "--limit", str(issue_limit),
                               "--json", "number,title,body"))
        if trigger == "github_issues":
            selected = []
            for issue in issues:
                detail = json.loads(gh(root, "issue", "view", str(issue["number"]),
                                       "--repo", repo["github"], "--json", "comments"))
                requests = [c for c in detail["comments"] if "/autofix" in c["body"]]
                if requests:
                    selected.append({**issue, "requests": requests})
            issues = selected
        contexts = [(issue, json.dumps(issue, ensure_ascii=False)) for issue in issues]
    elif template_name == "fix_ci_failure" or trigger == "github_ci":
        if not repo.get("github"):
            raise ValueError("fix_ci_failure requires repo.github and gh")
        filters = ["--branch", repo["base_branch"]] if trigger != "github_ci" else ["--event", "pull_request"]
        fields = "databaseId,headBranch,headSha"
        runs = json.loads(gh(root, "run", "list", "--repo", repo["github"], *filters,
                             "--status", "failure", "--limit", "20" if trigger == "github_ci" else "1",
                             "--json", fields))
        if trigger == "github_ci":
            prs = json.loads(gh(root, "pr", "list", "--repo", repo["github"], "--state", "open",
                                "--limit", "200", "--json", "number,headRefName,author,isCrossRepository"))
            allowed = {p["headRefName"] for p in prs
                       if not p["headRefName"].startswith("autofix/")
                       and not p["author"].get("is_bot", False)}
            runs = [r for r in runs if r["headBranch"] in allowed][:1]
        if runs:
            failed = runs[0]
            start_ref = failed["headSha"]
            if failed["headBranch"] != repo["base_branch"]:
                if trigger != "github_ci":
                    prs = json.loads(gh(root, "pr", "list", "--repo", repo["github"], "--state", "open",
                                        "--head", failed["headBranch"], "--json", "number,headRefName,isCrossRepository"))
                source_prs = [p for p in prs if p["headRefName"] == failed["headBranch"]]
                if not source_prs or any(p.get("isCrossRepository", False) for p in source_prs):
                    raise ValueError("CI repair requires a same-repository source PR for a non-base branch")
                publish_base = failed["headBranch"]
            exists = subprocess.run(["git", "cat-file", "-e", f"{start_ref}^{{commit}}"],
                                    cwd=root, capture_output=True)
            if exists.returncode:
                git(root, "fetch", "origin", start_ref)
                if git(root, "rev-parse", "FETCH_HEAD") != start_ref:
                    raise RuntimeError("Fetched CI commit does not match the failed run")
        contexts = [(None, gh(root, "run", "view", str(runs[0]["databaseId"]),
                              "--repo", repo["github"], "--log-failed")[-30000:])] if runs else []
    elif trigger == "github_push":
        if not repo.get("github"):
            raise ValueError("GitHub push polling requires repo.github and gh")
        contexts = [(None, gh(root, "api", f"repos/{repo['github']}/commits",
                              "--method", "GET", "-f", f"sha={repo['base_branch']}",
                              "-f", "per_page=1")[-30000:])]
    else:
        contexts = [(None, values["context"])]
    results = []
    for issue, context in contexts:
        agent = Agent(root, model, api_key)
        record = {"time": datetime.now(timezone.utc).isoformat(), "template": template_name,
                  "worktree": str(root),
                  "start_ref": start_ref, "publish_base": publish_base,
                  "repo": repo_name, "issue": issue["number"] if issue else None,
                  "status": "error"}
        try:
            rendered = {**values, "context": context}
            prompt = template["prompt"].format(**rendered)
            check = params.get("check_command", template.get("check_command"))
            check = check.format(**rendered) if check else None
            branch = prepare(root, template_name, start_ref)
            record["branch"] = branch
            summary = agent.run(prompt)
            if template.get("output_only"):
                reports = agent.path("reports")
                reports.mkdir(exist_ok=True)
                (reports / f"{branch.rsplit('/', 1)[-1]}.md").write_text(summary + "\n")
            outcome = complete(root, branch, publish_base, summary, check,
                               repo.get("github"), dry_run,
                               (lambda: review_tree(config, root, git(root, "diff",
                                   f"{start_ref}...HEAD"), log_dir))
                               if params.get("review_before_pr", repo.get("review_before_pr",
                                   config.get("review_before_pr", False))) else None)
            record.update(outcome, summary=summary, status="ok")
            if params.get("notify_command") and not dry_run:
                subprocess.run(params["notify_command"], shell=True, cwd=root, input=summary,
                               text=True, check=True, timeout=120, capture_output=True)
            if issue and outcome.get("pr"):
                gh(root, "issue", "comment", str(issue["number"]), "--repo", repo["github"],
                   "--body", f"Autofix proposed a fix: {outcome['pr']}")
            results.append(record)
        except Exception as exc:
            record["error"] = str(exc)
            raise
        finally:
            record["usage"] = agent.usage
            with (log_dir / "runs.jsonl").open("a") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    return results


def daemon(config, log_dir):
    log_dir = safe_log_dir(config, log_dir)
    due = {}
    while True:
        for index, automation in enumerate(config.get("automations", [])):
            if not automation.get("enabled", False):
                continue
            interval = float(automation["schedule_minutes"]) * 60
            if interval <= 0:
                raise ValueError("schedule_minutes must be positive")
            if time.monotonic() < due.get(index, 0):
                continue
            reserved = {"name", "template", "repo", "schedule_minutes", "enabled", "params"}
            params = {**automation.get("params", {}),
                      **{k: v for k, v in automation.items() if k not in reserved}}
            try:
                isolated_run(config, automation["template"], automation["repo"], params, log_dir=log_dir)
            except Exception as exc:
                log_dir.mkdir(parents=True, exist_ok=True)
                with (log_dir / "daemon.jsonl").open("a") as stream:
                    stream.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(),
                                             "automation": automation["name"], "error": str(exc)}) + "\n")
            due[index] = time.monotonic() + interval
        time.sleep(5)

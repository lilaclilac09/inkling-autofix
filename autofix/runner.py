import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .agent import Agent
from .gitops import complete, prepare


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
    template = templates()[template_name]
    repo = next(r for r in config["repos"] if r["name"] == repo_name)
    root = Path(repo["path"]).resolve()
    params = dict(params or {})
    values = {"task": "", "test_command": "python -m pytest -q",
              "lint_command": "ruff check . && mypy .", "context": "", **params}
    api_key = os.environ.get(config["model"].get("api_key_env", "INKLING_API_KEY"))
    if not api_key:
        raise ValueError("Missing API key environment variable")
    if template_name == "custom" and not values["task"].strip():
        raise ValueError("custom requires --task or automation task")
    log_dir.mkdir(parents=True, exist_ok=True)
    if template_name == "fix_github_issues":
        if not repo.get("github"):
            raise ValueError("fix_github_issues requires repo.github and gh")
        issues = json.loads(gh(root, "issue", "list", "--repo", repo["github"],
                               "--state", "open", "--label", "bug", "--json", "number,title,body"))
        contexts = [(issue, json.dumps(issue, ensure_ascii=False)) for issue in issues]
    elif template_name == "fix_ci_failure":
        if not repo.get("github"):
            raise ValueError("fix_ci_failure requires repo.github and gh")
        runs = json.loads(gh(root, "run", "list", "--repo", repo["github"], "--branch",
                             repo["base_branch"], "--status", "failure", "--limit", "1",
                             "--json", "databaseId"))
        contexts = [(None, gh(root, "run", "view", str(runs[0]["databaseId"]),
                              "--repo", repo["github"], "--log-failed")[-30000:])] if runs else []
    else:
        contexts = [(None, values["context"])]
    results = []
    for issue, context in contexts:
        agent = Agent(root, config["model"], api_key)
        record = {"time": datetime.now(timezone.utc).isoformat(), "template": template_name,
                  "repo": repo_name, "issue": issue["number"] if issue else None,
                  "status": "error"}
        try:
            rendered = {**values, "context": context}
            prompt = template["prompt"].format(**rendered)
            check = params.get("check_command", template.get("check_command"))
            check = check.format(**rendered) if check else None
            branch = prepare(root, template_name, repo["base_branch"])
            record["branch"] = branch
            summary = agent.run(prompt)
            outcome = complete(root, branch, repo["base_branch"], summary, check,
                               repo.get("github"), dry_run)
            record.update(outcome, summary=summary, status="ok")
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
                run(config, automation["template"], automation["repo"], params, log_dir=log_dir)
            except Exception as exc:
                log_dir.mkdir(parents=True, exist_ok=True)
                with (log_dir / "daemon.jsonl").open("a") as stream:
                    stream.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(),
                                             "automation": automation["name"], "error": str(exc)}) + "\n")
            due[index] = time.monotonic() + interval
        time.sleep(5)

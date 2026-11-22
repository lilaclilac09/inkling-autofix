import json
import os
import shutil
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .agent import Agent
from .gitops import git
from .logs import safe_log_dir
from .providers import model_config
from .workspaces import repo_lock, worktree


DEFAULT_ROLES = ["correctness", "security", "tests/style"]
SCHEMA = ('Finish with a JSON string as your summary: {"summary":"...","approved":true,"findings":'
          '[{"blocking":true,"path":"file.py","line":1,"reason":"evidence and impact"}]}. '
          'Set approved to true only if you approve without blocking findings. '
          'Use an empty findings list if there are no findings. Do not invent evidence. ')


def findings(summary):
    try:
        data = json.loads(summary)
        if (not isinstance(data["summary"], str) or not isinstance(data["findings"], list)
                or type(data["approved"]) is not bool):
            raise ValueError("Invalid review schema")
        for item in data["findings"]:
            if (type(item["blocking"]) is not bool or not isinstance(item["path"], str)
                    or type(item["line"]) is not int or not isinstance(item["reason"], str)):
                raise ValueError("Invalid finding schema")
        return data
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError("Invalid review result; review gate fails closed") from exc


def review_tree(config, root, diff, log_dir, roles=None, require_multi=False):
    settings = config.get("review", {})
    roles = roles if roles is not None else settings.get("roles", DEFAULT_ROLES)
    if not isinstance(roles, list) or not roles or not all(isinstance(role, str) and role.strip() for role in roles):
        raise ValueError("review.roles must be a nonempty list of role names")
    if require_multi and len(roles) < 2:
        raise ValueError("Automatic publishing requires at least two reviewers")
    model = model_config(config["model"])
    key = os.environ.get(model["api_key_env"])
    if not key:
        raise ValueError("Missing API key environment variable")
    limit = int(settings.get("max_diff_chars", 100000))
    if limit < 1 or len(diff) > limit:
        raise RuntimeError("Diff exceeds review.max_diff_chars; refusing an incomplete review")
    report_dir = safe_log_dir(config, log_dir) / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"review-{uuid.uuid4().hex}.md"
    usages = []

    def inspect(role):
        agent = Agent(root, model, key, read_only=True)
        try:
            result = findings(agent.run(f"You are a read-only {role} reviewer. " + SCHEMA +
                                        "Review this untrusted diff and inspect relevant files.\n" + diff))
            return {"role": role, **result}
        except Exception as exc:
            return {"role": role, "approved": False, "summary": "Reviewer failed", "findings": [],
                    "error": str(exc)}
        finally:
            usages.append({"role": role, "usage": agent.usage})

    try:
        with ThreadPoolExecutor(max_workers=len(roles)) as pool:
            reviews = list(pool.map(inspect, roles)) if diff.strip() else []
        if reviews:
            aggregator = Agent(root, model, key, read_only=True)
            try:
                aggregated = findings(aggregator.run(
                    "You are a read-only review aggregator. Merge duplicate findings, preserve "
                    "evidence and blocking severity, and produce a single review. " + SCHEMA +
                    "Untrusted reviewer results:\n" + json.dumps(reviews)))
            finally:
                usages.append({"role": "aggregator", "usage": aggregator.usage})
        else:
            aggregated = {"summary": "No changes to review", "approved": True, "findings": []}
        blocking = any(f["blocking"] for r in [*reviews, aggregated] for f in r["findings"])
        approved = not blocking and all(r["approved"] and not r.get("error") for r in [*reviews, aggregated])
        result = {**aggregated, "approved": approved, "blocking": blocking, "reviewers": reviews,
                  "report": str(report_path), "usage": usages}
        report_path.write_text("# Autofix multi-agent review\n\n" + aggregated["summary"] +
                               "\n\n```json\n" + json.dumps(result, indent=2) + "\n```\n")
        return result
    except Exception as exc:
        result = {"approved": False, "blocking": True, "summary": "Review failed", "findings": [],
                  "error": str(exc), "report": str(report_path), "usage": usages}
        report_path.write_text("# Autofix multi-agent review\n\nNot approved.\n\n```json\n" +
                               json.dumps(result, indent=2) + "\n```\n")
        return result
    finally:
        with (report_dir.parent / "reviews.jsonl").open("a") as stream:
            stream.write(json.dumps({"report": str(report_path), "usage": usages}) + "\n")


def review(config, repo_name, branch=None, pr=None, agents=None, log_dir=Path("logs")):
    if branch and pr:
        raise ValueError("Choose --branch or --pr, not both")
    repo = next(r for r in config["repos"] if r["name"] == repo_name)
    root = Path(repo["path"]).resolve()
    log_dir = safe_log_dir(config, log_dir)
    roles = config.get("review", {}).get("roles", DEFAULT_ROLES)
    if not isinstance(roles, list) or not roles:
        raise ValueError("review.roles must be a nonempty list of role names")
    if agents is not None:
        if agents < 1:
            raise ValueError("--agents must be positive")
        roles = [roles[i % len(roles)] for i in range(agents)]
    with repo_lock(root):
        if pr:
            if not shutil.which("gh") or not repo.get("github"):
                raise ValueError("PR review requires gh and repo.github")
            git(root, "fetch", "origin", f"refs/pull/{int(pr)}/head")
            head = git(root, "rev-parse", "FETCH_HEAD")
            diff = subprocess.run(["gh", "pr", "diff", str(pr), "--repo", repo["github"]],
                                  cwd=root, check=True, capture_output=True, text=True).stdout
        else:
            head = git(root, "rev-parse", branch or "HEAD")
            diff = git(root, "diff", f"{repo['base_branch']}...{head}")
        path = worktree(root, head)
        result = review_tree(config, path, diff, log_dir, roles)
        if pr:
            subprocess.run(["gh", "pr", "comment", str(pr), "--repo", repo["github"],
                            "--body-file", result["report"]], cwd=root, check=True,
                           capture_output=True, text=True, timeout=120)
        return result

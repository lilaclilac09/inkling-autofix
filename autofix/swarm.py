import json
import os
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from .agent import Agent, command
from .gitops import git
from .providers import model_config
from .runner import _run, templates
from .workspaces import repo_config, repo_lock, worktree
from .logs import safe_log_dir
from .review import review_tree
from .environment import command_env, model_commands


class SwarmFailure(RuntimeError):
    def __init__(self, result):
        self.result = result
        super().__init__(f"Swarm stopped without publishing: {result['error']}. Report: {result['report']}")


def stop_swarm(result, error, log_dir):
    reports = log_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"swarm-failed-{uuid.uuid4().hex}.md"
    result.update(status="failed", error=str(error), published=False, report=str(path))
    path.write_text("# Autofix swarm stopped\n\nNothing was published.\n\n```json\n" +
                    json.dumps(result, indent=2) + "\n```\n")
    with (log_dir / "swarms.jsonl").open("a") as stream:
        stream.write(json.dumps(result) + "\n")
    raise SwarmFailure(result)


@model_commands
def swarm(config, repo_name, jobs, agents=3, log_dir=Path("logs")):
    if agents < 1 or not jobs:
        raise ValueError("swarm requires jobs and agents >= 1")
    repo = next(r for r in config["repos"] if r["name"] == repo_name)
    root = Path(repo["path"]).resolve()
    log_dir = safe_log_dir(config, log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    with repo_lock(root):
        base = git(root, "rev-parse", repo["base_branch"])
        paths = [worktree(root, base) for _ in jobs]
        result = {"workers": [], "merged": [], "failures": [], "overlaps": {}, "checks": [],
                  "reviews": [], "published": False}

        def worker(index, job):
            name, params = job
            local = repo_config(config, repo_name, paths[index], base)
            return _run(local, name, repo_name, params, True, log_dir)

        with ThreadPoolExecutor(max_workers=agents) as pool:
            futures = {pool.submit(worker, i, job): i for i, job in enumerate(jobs)}
            records = {}
            for future in as_completed(futures):
                i = futures[future]
                try:
                    records[i] = future.result()
                    result["workers"].extend(records[i])
                except Exception as exc:
                    result["failures"].append({"worktree": str(paths[i]), "error": str(exc)})
        if result["failures"]:
            stop_swarm(result, "A worker failed; no partial integration is allowed", log_dir)

        integration = worktree(root, base)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        branch = f"autofix/swarm-{stamp}"
        git(integration, "switch", "-c", branch)
        result.update(branch=branch, worktree=str(integration))
        seen = {}
        model = model_config(config["model"])
        key = os.environ.get(model["api_key_env"])
        checks = []
        for i in sorted(records):
            name, params = jobs[i]
            template = templates()[name]
            values = {"test_command": "python -m pytest -q",
                      "lint_command": "ruff check . && mypy .", **params}
            check = params.get("check_command", template.get("check_command"))
            check = check.format(**values) if check else None
            if check and check not in checks:
                checks.append(check)
            for record in records[i]:
                if not record["changed"]:
                    continue
                candidate = record["branch"]
                files = set(git(root, "diff", "--name-only", base, candidate).splitlines())
                overlap = sorted(files & seen.keys())
                result["overlaps"][candidate] = overlap
                prior = git(integration, "rev-parse", "HEAD")
                merged = subprocess.run(["git", "merge", "--no-commit", "--no-ff", candidate],
                                        cwd=integration, capture_output=True, text=True, env=command_env())
                resolver = None
                try:
                    conflicts = git(integration, "diff", "--name-only", "--diff-filter=U").splitlines()
                    if merged.returncode and not conflicts:
                        raise RuntimeError(merged.stderr or merged.stdout)
                    if conflicts:
                        resolver = Agent(integration, model, key)
                        resolver.run("Resolve this swarm merge conflict without losing either agent's intent. "
                                     "Inspect the conflicted files and remove all conflict markers. "
                                     "Do not run Git commands; the runner stages the resolution. "
                                     f"Files: {json.dumps(conflicts)}. Check command: {check or 'none'}. "
                                     f"Candidate summary: {record['summary']}")
                        for file in conflicts:
                            path = resolver.path(file)
                            if path.exists() and any(line.startswith(("<<<<<<< ", "=======", ">>>>>>> "))
                                                     for line in path.read_text().splitlines()):
                                raise RuntimeError(f"Unresolved conflict markers in {file}")
                        git(integration, "add", "--all")
                    if git(integration, "branch", "--show-current") != branch:
                        raise RuntimeError("Resolver changed the integration branch")
                    for active_check in checks:
                        checked = command(integration, active_check)
                        result["checks"].append({"branch": candidate, "command": active_check, "result": checked})
                        if checked["returncode"]:
                            raise RuntimeError(f"Integration check failed: {checked['output']}")
                    if git(integration, "branch", "--show-current") != branch:
                        raise RuntimeError("Check changed the integration branch")
                    git(integration, "add", "--all")
                    review = review_tree(config, integration, git(integration, "diff", base),
                                         log_dir, require_multi=True)
                    result["reviews"].append(review)
                    if not review["approved"] or review["blocking"]:
                        raise RuntimeError("Integration did not receive unanimous review approval")
                    git(integration, "commit", "-m", f"autofix: integrate {candidate}")
                    result["merged"].append(candidate)
                    seen.update({file: candidate for file in files})
                except Exception as exc:
                    # Abort only the in-progress merge in this dedicated integration worktree.
                    abort = subprocess.run(["git", "merge", "--abort"], cwd=integration,
                                           capture_output=True, text=True, env=command_env())
                    result["failures"].append({"branch": candidate, "error": str(exc)})
                    if (abort.returncode or git(integration, "rev-parse", "HEAD") != prior
                            or git(integration, "status", "--porcelain")):
                        stop_swarm(result, f"{exc}; could not safely restore integration; inspect manually", log_dir)
                    stop_swarm(result, exc, log_dir)
                finally:
                    if resolver:
                        with (log_dir / "resolvers.jsonl").open("a") as stream:
                            stream.write(json.dumps({"branch": candidate, "usage": resolver.usage}) + "\n")
        result["status"] = "local-only"
        with (log_dir / "swarms.jsonl").open("a") as stream:
            stream.write(json.dumps(result) + "\n")
        return result

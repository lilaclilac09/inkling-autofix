import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from .agent import Agent, command
from .gitops import git
from .providers import model_config
from .runner import _run, templates
from .workspaces import repo_config, repo_lock, worktree


def swarm(config, repo_name, jobs, agents=3, log_dir=Path("logs")):
    if agents < 1 or not jobs:
        raise ValueError("swarm requires jobs and agents >= 1")
    repo = next(r for r in config["repos"] if r["name"] == repo_name)
    root = Path(repo["path"]).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    with repo_lock(root):
        base = git(root, "rev-parse", repo["base_branch"])
        paths = [worktree(root, base) for _ in jobs]
        result = {"workers": [], "merged": [], "skipped": [], "overlaps": {}, "checks": []}

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
                    result["skipped"].append({"worktree": str(paths[i]), "error": str(exc)})

        integration = worktree(root, base)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        branch = f"autofix/swarm-{stamp}"
        git(integration, "switch", "-c", branch)
        result.update(branch=branch, worktree=str(integration))
        seen = {}
        model = model_config(config["model"])
        key = os.environ.get(model["api_key_env"])
        for i in sorted(records):
            name, params = jobs[i]
            template = templates()[name]
            values = {"test_command": "python -m pytest -q",
                      "lint_command": "ruff check . && mypy .", **params}
            check = params.get("check_command", template.get("check_command"))
            check = check.format(**values) if check else None
            for record in records[i]:
                if not record["changed"]:
                    continue
                candidate = record["branch"]
                files = set(git(root, "diff", "--name-only", base, candidate).splitlines())
                overlap = sorted(files & seen.keys())
                result["overlaps"][candidate] = overlap
                prior = git(integration, "rev-parse", "HEAD")
                merged = subprocess.run(["git", "merge", "--no-commit", "--no-ff", candidate],
                                        cwd=integration, capture_output=True, text=True)
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
                    checked = command(integration, check) if check else None
                    result["checks"].append({"branch": candidate, "result": checked})
                    if checked and checked["returncode"]:
                        raise RuntimeError(f"Integration check failed: {checked['output']}")
                    if git(integration, "branch", "--show-current") != branch:
                        raise RuntimeError("Check changed the integration branch")
                    git(integration, "add", "--all")
                    git(integration, "commit", "-m", f"autofix: integrate {candidate}")
                    result["merged"].append(candidate)
                    seen.update({file: candidate for file in files})
                except Exception as exc:
                    # Abort only the in-progress merge in this dedicated integration worktree.
                    abort = subprocess.run(["git", "merge", "--abort"], cwd=integration,
                                           capture_output=True, text=True)
                    result["skipped"].append({"branch": candidate, "error": str(exc)})
                    if abort.returncode or git(integration, "rev-parse", "HEAD") != prior:
                        result["error"] = "Could not safely restore integration; manual inspection required"
                        return result
                finally:
                    if resolver:
                        with (log_dir / "resolvers.jsonl").open("a") as stream:
                            stream.write(json.dumps({"branch": candidate, "usage": resolver.usage}) + "\n")
        with (log_dir / "swarms.jsonl").open("a") as stream:
            stream.write(json.dumps(result) + "\n")
        return result

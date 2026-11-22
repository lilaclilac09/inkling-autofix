import re
import shutil
import subprocess
from datetime import datetime, timezone

from .agent import command


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout.strip()


def prepare(root, template, base):
    if git(root, "status", "--porcelain"):
        raise RuntimeError("Repository must be clean before a run")
    if git(root, "rev-parse", "--show-toplevel") != str(root):
        raise RuntimeError("Configured path must be the Git repository root")
    git(root, "switch", base)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    slug = re.sub(r"[^a-zA-Z0-9_-]", "-", template)
    branch = f"autofix/{slug}-{stamp}"
    git(root, "switch", "-c", branch)
    return branch


def complete(root, branch, base, summary, check, github, dry_run):
    if git(root, "branch", "--show-current") != branch or branch == base:
        raise RuntimeError("Branch changed unexpectedly; refusing publication")
    result = command(root, check) if check else None
    if result and result["returncode"]:
        raise RuntimeError(f"Check failed; changes left uncommitted: {result['output']}")
    if git(root, "branch", "--show-current") != branch:
        raise RuntimeError("Check changed branch; refusing commit or publication")
    if not git(root, "status", "--porcelain"):
        return {"changed": False, "check": result}
    git(root, "add", "--all")
    git(root, "commit", "-m", "autofix: " + summary.splitlines()[0][:100])
    pr = None
    if github and shutil.which("gh") and not dry_run:
        git(root, "push", "-u", "origin", f"HEAD:refs/heads/{branch}")
        pr = subprocess.run(["gh", "pr", "create", "--repo", github, "--base", base,
                             "--head", branch, "--title", "autofix: " + summary.splitlines()[0][:100],
                             "--body", summary], cwd=root, check=True,
                            capture_output=True, text=True).stdout.strip()
    return {"changed": True, "check": result, "pr": pr}

import re
import shutil
import subprocess
import json
from datetime import datetime, timezone
from pathlib import Path

from .agent import command


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout.strip()


def prepare(root, template, base):
    if git(root, "status", "--porcelain"):
        raise RuntimeError("Repository must be clean before a run")
    if git(root, "rev-parse", "--show-toplevel") != str(root):
        raise RuntimeError("Configured path must be the Git repository root")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    slug = re.sub(r"[^a-zA-Z0-9_-]", "-", template)
    branch = f"autofix/{slug}-{stamp}"
    git(root, "switch", "-c", branch, base)
    return branch


def complete(root, branch, base, summary, check, github, dry_run, review_callback=None):
    summary = summary.strip() or "Complete automated task"
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
    publish_requested = bool(github and not dry_run)
    review = None
    if review_callback or publish_requested:
        try:
            if review_callback is None:
                raise RuntimeError("Automatic publishing requires unanimous multi-agent review")
            review = review_callback()
        except Exception as exc:
            common = Path(git(root, "rev-parse", "--git-common-dir"))
            if not common.is_absolute():
                common = root / common
            reports = common / "autofix-logs" / "reports"
            reports.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
            path = reports / f"review-error-{stamp}.md"
            review = {"approved": False, "blocking": True, "error": str(exc), "report": str(path)}
            path.write_text("# Autofix review not approved\n\n" + json.dumps(review, indent=2) + "\n")
    if review is not None and (review.get("approved") is not True or review.get("blocking") is not False):
        return {"changed": True, "check": result, "pr": None, "review": review,
                "publication_blocked": True}
    pr = None
    if github and shutil.which("gh") and not dry_run:
        git(root, "push", "-u", "origin", f"HEAD:refs/heads/{branch}")
        pr = subprocess.run(["gh", "pr", "create", "--repo", github, "--base", base,
                             "--head", branch, "--title", "autofix: " + summary.splitlines()[0][:100],
                             "--body", summary], cwd=root, check=True,
                            capture_output=True, text=True).stdout.strip()
    return {"changed": True, "check": result, "pr": pr, "review": review}

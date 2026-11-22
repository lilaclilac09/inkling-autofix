from pathlib import Path

from .gitops import git


def safe_log_dir(config, requested):
    path = Path(requested).resolve()
    for repo in config["repos"]:
        root = Path(repo["path"]).resolve()
        if path.is_relative_to(root):
            common = Path(git(root, "rev-parse", "--git-common-dir"))
            if not common.is_absolute():
                common = root / common
            return common.resolve() / "autofix-logs"
    return path

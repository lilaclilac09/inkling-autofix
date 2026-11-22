import fcntl
import uuid
from contextlib import contextmanager
from pathlib import Path

from .gitops import git


@contextmanager
def repo_lock(root):
    common = Path(git(root, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = Path(root) / common
    with (common.resolve() / "autofix.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Repository is locked by another autofix run") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def worktree(root, ref):
    root = Path(root).resolve()
    parent = root.parent / f".{root.name}-autofix-worktrees"
    parent.mkdir(exist_ok=True)
    path = parent / uuid.uuid4().hex
    git(root, "worktree", "add", "--detach", str(path), ref)
    return path


def repo_config(config, repo_name, path, base=None):
    repos = [{**r, "path": str(path), **({"start_ref": base} if base else {})}
             if r["name"] == repo_name else dict(r) for r in config["repos"]]
    return {**config, "repos": repos}

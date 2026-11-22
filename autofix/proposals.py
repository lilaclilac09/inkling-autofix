import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

import yaml

from .agent import Agent, command
from .environment import command_env, key_environment
from .providers import model_config


SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
        ".mypy_cache", ".ruff_cache", ".tox", ".nox", ".DS_Store"}


class ScopeError(ValueError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def relative_path(value, allow_root=False):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("Invalid relative path")
    path = PurePosixPath(value)
    if (path.is_absolute() or ".." in path.parts or path.as_posix() != value
            or any(part in SKIP for part in path.parts)
            or (value == "." and not allow_root)):
        raise ValueError(f"Unsafe relative path: {value}")
    return path


def safe_path(root, value):
    relative = relative_path(value)
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f"Symlinks are not supported: {value}")
        if path != root / relative and path.exists() and not path.is_dir():
            raise ValueError(f"Parent is not a directory: {value}")
    if not path.resolve().is_relative_to(root):
        raise ValueError(f"Path escapes root: {value}")
    return path


def in_scope(value, scope):
    path = relative_path(value)
    selected = relative_path(scope["path"], allow_root=True)
    if scope["kind"] == "file":
        return path == selected
    if scope["kind"] != "directory":
        raise ValueError("Invalid scope kind")
    return selected == PurePosixPath(".") or selected == path or selected in path.parents


def snapshot_files(root):
    files = {}
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name not in SKIP]
        for name in [*dirs, *names]:
            if name in SKIP:
                continue
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError(f"Symlinks are not supported: {path.relative_to(root)}")
            if path.is_dir():
                continue
            if not path.is_file():
                raise ValueError(f"Only regular files are supported: {path.relative_to(root)}")
            files[path.relative_to(root).as_posix()] = {
                "sha256": digest(path.read_bytes()), "mode": stat.S_IMODE(path.stat().st_mode) & 0o777}
    return files


def workspace_git(root, *args):
    env = {key: value for key, value in command_env().items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.run(["git", "-c", f"core.hooksPath={os.devnull}", "-c", "commit.gpgSign=false",
                           "-c", "user.name=Autofix", "-c", "user.email=autofix@localhost", *args],
                          cwd=root, env=env, check=True, capture_output=True, text=True).stdout


def proposal_model(path=None):
    settings = yaml.safe_load(Path(path).read_text()) if path else {}
    if not isinstance(settings, dict):
        raise ValueError("Model config must be a YAML mapping of model settings")
    return model_config({"provider": "tinker", "model": "thinkingmachines/Inkling:peft:262144", **settings})


def propose(path, out, task=None, fix_tests=False, test_command=None, model_file=None):
    if bool(task and task.strip()) == bool(fix_tests):
        raise ValueError("Choose exactly one nonempty task or fix-tests")
    if test_command and not fix_tests:
        raise ValueError("test-command requires fix-tests")
    selected = Path(path).expanduser()
    if selected.is_symlink():
        raise ValueError("Select a regular file or directory, not a symlink")
    selected = selected.resolve(strict=True)
    if ".git" in selected.parts or not (selected.is_file() or selected.is_dir()):
        raise ValueError("Select a project folder or regular file outside .git")
    output = Path(out).expanduser().resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("Proposal output must be a new or empty directory")
    model = proposal_model(model_file)
    key = os.environ.get(model["api_key_env"])
    if not key:
        raise ValueError(f"Missing API key environment variable: {model['api_key_env']}")
    with key_environment(model["api_key_env"]):
        parent = selected if selected.is_dir() else selected.parent
        try:
            root = Path(workspace_git(parent, "rev-parse", "--show-toplevel").strip()).resolve()
        except subprocess.CalledProcessError:
            root = parent
        scope = {"path": selected.relative_to(root).as_posix(),
                 "kind": "directory" if selected.is_dir() else "file"}
        relative_path(scope["path"], allow_root=True)
        with tempfile.TemporaryDirectory(prefix="autofix-proposal-") as temporary:
            temporary_root = Path(temporary).resolve()
            workspace = temporary_root / "workspace"

            def ignore(directory, names):
                return [name for name in names if name in SKIP or (Path(directory) / name).is_symlink()
                        or Path(directory) / name == temporary_root]

            shutil.copytree(root, workspace, ignore=ignore)
            original = snapshot_files(workspace)
            workspace_git(workspace, "init")
            workspace_git(workspace, "add", "--force", "--all", "--", ".")
            workspace_git(workspace, "commit", "--allow-empty", "-m", "Proposal baseline")
            baseline = workspace_git(workspace, "rev-parse", "HEAD").strip()
            check = (test_command or "python -m pytest -q") if fix_tests else None
            prompt = (f"Fix the failing tests. Test command: {check}" if fix_tests else task)
            prompt += (f"\nAllowed edit scope: {scope['kind']} {scope['path']}. "
                       "Inspect other files for context but change only this scope. "
                       "This is a disposable copy. Propose a minimal fix; do not commit, push, or publish. "
                       "Do not create symlinks or access files outside this workspace.")
            agent = Agent(workspace, model, key)
            summary = agent.run(prompt)
            checked = command(workspace, check) if check else None
            updated = snapshot_files(workspace)
            changed = sorted(name for name in original.keys() | updated.keys()
                             if original.get(name) != updated.get(name))
            outside = [name for name in changed if not in_scope(name, scope)]
            if outside:
                raise ScopeError("Out-of-scope changes:\n" + "\n".join(outside))
            if checked and checked["returncode"]:
                raise RuntimeError(f"Proposal check failed: {checked['output']}")
            workspace_git(workspace, "add", "--force", "--all", "--", ".")
            diff = workspace_git(workspace, "diff", "--cached", "--binary", "--no-ext-diff",
                                 "--no-textconv", baseline, "--")
            output.mkdir(parents=True, exist_ok=True)
            contents = output / "contents"
            contents.mkdir()
            entries = []
            for index, name in enumerate(changed):
                old, new = original.get(name), updated.get(name)
                entry = {"path": name, "action": "delete" if new is None else "add" if old is None else "modify",
                         "original_sha256": old["sha256"] if old else None}
                if new is not None:
                    payload = f"contents/{index}"
                    (output / payload).write_bytes((workspace / name).read_bytes())
                    entry.update(contents=payload, new_sha256=new["sha256"], mode=new["mode"])
                entries.append(entry)
            (output / "proposal.diff").write_text(diff)
            (output / "summary.txt").write_text(summary + "\n")
            manifest = {"version": 1, "root": str(root), "scope": scope, "files": entries}
            (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
            return manifest


def apply_proposal(proposal):
    directory = Path(proposal).expanduser().resolve(strict=True)
    manifest = json.loads(safe_path(directory, "manifest.json").read_text())
    if manifest.get("version") != 1 or not isinstance(manifest.get("files"), list):
        raise ValueError("Invalid proposal manifest")
    root = Path(manifest["root"])
    if not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir():
        raise ValueError("Proposal root must still be the same real directory")
    scope = manifest["scope"]
    relative_path(scope["path"], allow_root=scope["kind"] == "directory")
    plans, stale, seen = [], [], set()
    for entry in manifest["files"]:
        name, action = entry["path"], entry["action"]
        if not in_scope(name, scope):
            raise ScopeError(f"Out-of-scope change: {name}")
        if name in seen or action not in {"add", "modify", "delete"}:
            raise ValueError(f"Invalid or duplicate proposal entry: {name}")
        seen.add(name)
        destination = safe_path(root, name)
        expected = entry["original_sha256"]
        if (action == "add") != (expected is None):
            raise ValueError(f"Invalid original hash for {name}")
        if (destination.exists() and not destination.is_file()
                or (digest(destination.read_bytes()) if destination.is_file() else None) != expected):
            stale.append(name)
        data, mode = None, None
        if action != "delete":
            data = safe_path(directory, entry["contents"]).read_bytes()
            mode = entry["mode"]
            if (digest(data) != entry["new_sha256"] or type(mode) is not int or not 0 <= mode <= 0o777):
                raise ValueError(f"Corrupt proposal contents or mode: {name}")
        plans.append((destination, data, mode))
    if stale:
        raise ValueError("Original files changed since proposal; nothing applied:\n" + "\n".join(stale))
    for destination, data, mode in plans:
        safe_path(root, destination.relative_to(root).as_posix())
        if data is None:
            destination.unlink()
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=".autofix-", dir=destination.parent)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                os.chmod(temporary, mode)
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
    return {"applied": len(plans), "root": str(root)}

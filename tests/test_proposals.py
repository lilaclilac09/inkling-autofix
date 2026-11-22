import json
import os
import shlex
import subprocess
import sys
import tempfile
import stat
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
import yaml

from autofix import cli, proposals, runner
from autofix.agent import command
from autofix.gitops import git
from autofix.proposals import VALIDATION, ScopeError, apply_proposal, digest, proposal_model, propose
from autofix.runner import isolated_run, run
from autofix.swarm import swarm
from test_runner import config, fake_model, repo


@pytest.fixture
def model_file(tmp_path, fake_model, monkeypatch):
    monkeypatch.setenv("PROJECT_MODEL_TOKEN", "fake-model-key")
    path = tmp_path / "model.yaml"
    path.write_text(yaml.safe_dump({**fake_model["model"], "provider": "openai",
                                    "api_key_env": "PROJECT_MODEL_TOKEN"}))
    return path


def test_propose_folder_and_apply_add_modify_delete(tmp_path, fake_model, model_file):
    project = tmp_path / "project"
    project.mkdir()
    (project / "code.py").write_text("broken\n")
    (project / "obsolete.txt").write_text("obsolete\n")
    (project / "node_modules").mkdir()
    (project / "node_modules" / "cache.txt").write_text("leave alone")
    fake_model["script"] = [
        ("write_file", {"path": "code.py", "content": "fixed\n"}),
        ("write_file", {"path": "new.txt", "content": "new\n"}),
        ("delete_file", {"path": "obsolete.txt"}),
        ("finish", {"summary": "Fix code and remove obsolete data"}),
    ]
    out = tmp_path / "proposal"
    manifest = propose(project, out, task="Fix code", model_file=model_file)
    assert manifest["root"] == str(project)
    assert manifest["scope"] == {"kind": "directory", "path": "."}
    assert (project / "code.py").read_text() == "broken\n"
    assert (project / "obsolete.txt").exists()
    assert not (project / "new.txt").exists()
    assert not (project / ".git").exists()
    changes = {entry["path"]: entry for entry in manifest["files"]}
    assert changes["code.py"]["original_sha256"] == digest(b"broken\n")
    assert changes["new.txt"]["original_sha256"] is None
    assert changes["obsolete.txt"]["action"] == "delete"
    diff = (out / "proposal.diff").read_text()
    assert "-broken" in diff and "+fixed" in diff
    assert (out / "summary.txt").read_text() == "Fix code and remove obsolete data\n\n" + VALIDATION + "\n"
    assert apply_proposal(out)["applied"] == 3
    assert (project / "code.py").read_text() == "fixed\n"
    assert (project / "new.txt").read_text() == "new\n"
    assert not (project / "obsolete.txt").exists()
    assert (project / "node_modules" / "cache.txt").read_text() == "leave alone"
    assert not (project / ".git").exists()


def test_empty_proposal(tmp_path, fake_model, model_file):
    project = tmp_path / "empty"
    project.mkdir()
    fake_model["script"] = [("finish", {"summary": "No changes necessary"})]
    out = tmp_path / "proposal"
    assert propose(project, out, task="Inspect", model_file=model_file)["files"] == []
    assert (out / "proposal.diff").read_text() == ""
    assert apply_proposal(out)["applied"] == 0
    assert list(project.iterdir()) == []


def test_non_utf8_input_is_skipped_as_binary_without_diff_failure(tmp_path, fake_model, model_file):
    project = tmp_path / "project"
    project.mkdir()
    (project / "notes.txt").write_bytes("caf\u00e9".encode("latin-1"))
    (project / "code.txt").write_text("before\n")
    fake_model["script"] = [("read_file", {"path": "notes.txt"}),
                            ("write_file", {"path": "code.txt", "content": "after\n"}),
                            ("finish", {"summary": "Propose a text fix"})]
    out = tmp_path / "proposal"
    manifest = propose(project, out, task="Fix code", model_file=model_file)
    assert manifest["skipped_paths"] == ["notes.txt"]
    assert "Skipped path is unavailable" in fake_model["calls"][1]["messages"][-1]["content"]
    assert "caf" not in json.dumps(fake_model["calls"])
    assert "+after" in (out / "proposal.diff").read_text()
    apply_proposal(out)
    assert (project / "notes.txt").read_bytes() == b"caf\xe9"


def test_private_proposal_permissions_and_original_mode(tmp_path, fake_model, model_file):
    project = tmp_path / "project"
    project.mkdir()
    target = project / "code.txt"
    target.write_text("original")
    target.chmod(0o640)
    fake_model["script"] = [("write_file", {"path": "code.txt", "content": "fixed"}),
                            ("finish", {"summary": "Fix"})]
    out = tmp_path / "new-parent/proposal"
    previous = os.umask(0)
    try:
        manifest = propose(project, out, task="Fix", model_file=model_file)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    for path in out.rglob("*"):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    manifest["files"][0]["mode"] = 0o777
    (out / "manifest.json").write_text(json.dumps(manifest))
    apply_proposal(out)
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_concurrent_apply_serializes_hash_validation_and_writes(tmp_path, fake_model, model_file, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    project.mkdir()
    (project / "code.txt").write_text("original")
    fake_model["script"] = [("write_file", {"path": "code.txt", "content": "fixed"}),
                            ("finish", {"summary": "Fix"})]
    out = tmp_path / "proposal"
    propose(project, out, task="Fix", model_file=model_file)
    replacing, release, second_started, second_hashed = [threading.Event() for _ in range(4)]
    local = threading.local()
    original_replace, original_digest = os.replace, proposals.digest
    def delayed_replace(*args):
        if local.name == "first":
            replacing.set()
            assert release.wait(5)
        return original_replace(*args)
    def tracked_digest(data):
        if getattr(local, "name", None) == "second" and data == b"original":
            second_hashed.set()
        return original_digest(data)
    def apply(name):
        local.name = name
        if name == "second":
            second_started.set()
        return apply_proposal(out)
    monkeypatch.setattr(os, "replace", delayed_replace)
    monkeypatch.setattr(proposals, "digest", tracked_digest)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(apply, "first")
        try:
            assert replacing.wait(5)
            second = pool.submit(apply, "second")
            assert second_started.wait(5)
            assert not second_hashed.wait(0.2)
        finally:
            release.set()
        assert first.result(timeout=5)["applied"] == 1
        with pytest.raises(ValueError, match="Original files changed"):
            second.result(timeout=5)
    assert (project / "code.txt").read_text() == "fixed"


def test_temporary_workspace_inside_source_via_symlink_is_not_recopied(tmp_path, fake_model, model_file, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "code.txt").write_text("original")
    alias = tmp_path / "temp-alias"
    alias.symlink_to(project, target_is_directory=True)
    monkeypatch.setattr(tempfile, "tempdir", str(alias))
    fake_model["script"] = [("finish", {"summary": "No changes"})]
    manifest = propose(project, tmp_path / "proposal", task="Inspect", model_file=model_file)
    assert manifest["files"] == []
    assert [file.name for file in project.iterdir()] == ["code.txt"]


@pytest.mark.parametrize("stale", ["modified", "removed", "created"])
def test_apply_preflights_all_files_before_writing(tmp_path, fake_model, model_file, stale):
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.txt").write_text("original a")
    if stale != "created":
        (project / "b.txt").write_text("original b")
    fake_model["script"] = [("write_file", {"path": name, "content": "proposed"})
                             for name in ["a.txt", "b.txt"]] + [("finish", {"summary": "Changes"})]
    out = tmp_path / "proposal"
    propose(project, out, task="Fix", model_file=model_file)
    if stale == "removed":
        (project / "b.txt").unlink()
    else:
        (project / "b.txt").write_text("user's concurrent edit")
    with pytest.raises(ValueError, match="b.txt"):
        apply_proposal(out)
    assert (project / "a.txt").read_text() == "original a"


@pytest.mark.parametrize("single_file", [False, True])
def test_scoped_git_proposal_keeps_real_branch_and_dirty_files(repo, tmp_path, fake_model, model_file, single_file):
    folder = repo / "package"
    folder.mkdir()
    target = folder / "code.py"
    target.write_text("original")
    before = git(repo, "status", "--porcelain")
    head = git(repo, "rev-parse", "HEAD")
    fake_model["script"] = [("write_file", {"path": "package/code.py", "content": "fixed"}),
                            ("finish", {"summary": "Fix selected file"})]
    out = tmp_path / "proposal"
    manifest = propose(target if single_file else folder, out, task="Fix", model_file=model_file)
    assert manifest["root"] == str(repo)
    assert manifest["scope"] == {"path": "package/code.py" if single_file else "package",
                                  "kind": "file" if single_file else "directory"}
    assert target.read_text() == "original"
    assert git(repo, "status", "--porcelain") == before
    assert git(repo, "rev-parse", "HEAD") == head
    assert "Allowed edit scope" in fake_model["calls"][0]["messages"][1]["content"]
    apply_proposal(out)
    assert target.read_text() == "fixed"
    assert git(repo, "branch", "--show-current") == "main"
    assert git(repo, "rev-parse", "HEAD") == head


def test_single_file_without_git_via_cli(tmp_path, fake_model, model_file, monkeypatch):
    target = tmp_path / "code.txt"
    target.write_text("broken")
    out = tmp_path / "proposal"
    fake_model["script"] = [("write_file", {"path": "code.txt", "content": "fixed"}),
                            ("finish", {"summary": "Fix"})]
    monkeypatch.setattr(sys, "argv", ["autofix", "propose", "--path", str(target), "--task", "Fix",
                                     "--out", str(out), "--model-config", str(model_file)])
    cli.main()
    assert target.read_text() == "broken"
    monkeypatch.setattr(sys, "argv", ["autofix", "apply", "--proposal", str(out)])
    cli.main()
    assert target.read_text() == "fixed"


@pytest.mark.parametrize("single_file", [False, True])
def test_out_of_scope_cli_exit_three_no_applicable_output(repo, tmp_path, fake_model, model_file,
                                                          monkeypatch, capsys, single_file):
    folder = repo / "selected"
    folder.mkdir()
    target = folder / "code.txt"
    target.write_text("unchanged")
    fake_model["script"] = [("write_file", {"path": "calc.py", "content": "out of scope"}),
                            ("finish", {"summary": "Fix"})]
    out = tmp_path / "proposal"
    monkeypatch.setattr(sys, "argv", ["autofix", "propose", "--path", str(target if single_file else folder),
                                     "--task", "Fix", "--out", str(out), "--model-config", str(model_file)])
    with pytest.raises(SystemExit) as status:
        cli.main()
    assert status.value.code == 3
    assert "calc.py" in capsys.readouterr().err
    assert not (out / "manifest.json").exists()
    assert "return a - b" in (repo / "calc.py").read_text()
    assert target.read_text() == "unchanged"


def test_fix_tests_uses_output_and_never_runs_commands(repo, tmp_path, fake_model, model_file, monkeypatch):
    marker = tmp_path / "command-ran"
    output = tmp_path / "failing-output.txt"
    output.write_text("AssertionError: expected add(2, 3) == 5\n")
    fake_model["script"] = [("run_command", {"command": f"touch {shlex.quote(str(marker))}"}),
                            ("finish", {"summary": "Unvalidated proposal"})]
    out = tmp_path / "proposal"
    def forbidden(*args, **kwargs):
        raise AssertionError("Proposal attempted to start a subprocess")
    with monkeypatch.context() as context:
        context.setattr(subprocess, "Popen", forbidden)
        with pytest.warns(UserWarning, match="ignored"):
            manifest = propose(repo, out, fix_tests=True, test_output=output,
                               test_command=f"touch {shlex.quote(str(marker))}", model_file=model_file)
    assert manifest["files"] == []
    assert not marker.exists()
    assert "run_command" not in [tool["function"]["name"] for tool in fake_model["calls"][0]["tools"]]
    assert "Unavailable tool: run_command" in fake_model["calls"][1]["messages"][-1]["content"]
    assert output.read_text().strip() in fake_model["calls"][0]["messages"][1]["content"]
    assert VALIDATION in (out / "summary.txt").read_text()
    assert "return a - b" in (repo / "calc.py").read_text()
    assert git(repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("tamper", ["scope", "traversal", "symlink", "payload"])
def test_apply_rejects_unsafe_or_corrupt_proposals(tmp_path, fake_model, model_file, tamper):
    project = tmp_path / "project"
    project.mkdir()
    target = project / "code.txt"
    target.write_text("original")
    external = tmp_path / "external.txt"
    external.write_text("external")
    fake_model["script"] = [("write_file", {"path": "code.txt", "content": "fixed"}),
                            ("finish", {"summary": "Fix"})]
    out = tmp_path / "proposal"
    manifest = propose(target, out, task="Fix", model_file=model_file)
    if tamper == "scope":
        manifest["files"][0]["path"] = "other.txt"
    elif tamper == "traversal":
        manifest["files"][0]["path"] = "../external.txt"
    elif tamper == "symlink":
        target.unlink()
        target.symlink_to(external)
    else:
        (out / manifest["files"][0]["contents"]).write_text("corrupted")
    (out / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        apply_proposal(out)
    assert external.read_text() == "external"
    if tamper != "symlink":
        assert target.read_text() == "original"


def test_default_tinker_model():
    settings = proposal_model()
    assert settings["provider"] == "tinker"
    assert settings["api_key_env"] == "TINKER_API_KEY"
    assert settings["model"] == "thinkingmachines/Inkling:peft:262144"


def test_out_of_scope_attempt_is_fatal_even_after_recovery(repo, tmp_path, fake_model, model_file):
    fake_model["script"] = [("write_file", {"path": "test_calc.py", "content": "outside scope"}),
                            ("finish", {"summary": "Attempt"})]
    out = tmp_path / "proposal"
    with pytest.raises(ScopeError, match="test_calc.py"):
        propose(repo / "calc.py", out, task="Fix", model_file=model_file)
    assert not (out / "manifest.json").exists()


def secret_check():
    names = ["PROJECT_MODEL_TOKEN", "TINKER_API_KEY", "INKLING_API_KEY", "OPENAI_API_KEY", "AUTOFIX_TEST_KEY_X"]
    script = f"import os; assert all(name not in os.environ for name in {names!r}); "
    script += "assert os.environ['KEEP_ME'] == 'available'; print('keys absent')"
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


def set_keys(monkeypatch):
    for name in ["PROJECT_MODEL_TOKEN", "TINKER_API_KEY", "INKLING_API_KEY", "OPENAI_API_KEY", "AUTOFIX_TEST_KEY_X"]:
        monkeypatch.setenv(name, "fake-model-key")
    monkeypatch.setenv("KEEP_ME", "available")


def test_proposal_filters_inputs_and_never_copies_links_or_special_files(tmp_path, fake_model, model_file, monkeypatch):
    set_keys(monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    (project / "code.txt").write_text("visible")
    hidden = [".env", ".env.local", "cert.pem", "private.key", "bundle.p12", "bundle.pfx", "id_rsa",
              "id_ed25519.pub", ".netrc", ".npmrc", ".pypirc", "credentials.json", "secrets.txt"]
    for name in hidden:
        (project / name).write_text("fixture-sensitive-text-never-send")
    for name in [".git", ".ssh", ".aws", ".venv", "venv", "node_modules", "__pycache__"]:
        (project / name).mkdir()
        (project / name / "hidden.txt").write_text("fixture-sensitive-text-never-send")
    (project / "binary.dat").write_bytes(b"\x00\xff")
    (project / "large.txt").write_bytes(b"x" * (1024 * 1024 + 1))
    external = tmp_path / "external.txt"
    external.write_text("fixture-external-text-never-send")
    (project / "link.txt").symlink_to(external)
    (project / "linked-dir").symlink_to(tmp_path, target_is_directory=True)
    os.mkfifo(project / "pipe")
    copied = {}
    original_copy = proposals.copy_scope
    def capture(*args):
        result = original_copy(*args)
        copied.update({str(p.relative_to(args[2])): p.read_bytes() for p in args[2].rglob("*") if p.is_file()})
        assert not any(p.is_symlink() for p in args[2].rglob("*"))
        return result
    monkeypatch.setattr(proposals, "copy_scope", capture)
    fake_model["script"] = [("list_files", {}), ("read_file", {"path": ".env"}),
                            ("read_file", {"path": "link.txt"}), ("finish", {"summary": "Inspected"})]
    manifest = propose(project, tmp_path / "proposal", task="Inspect", model_file=model_file)
    assert copied == {"code.txt": b"visible"}
    assert set(hidden + ["link.txt", "linked-dir", "pipe", "binary.dat", "large.txt"]).issubset(manifest["skipped_paths"])
    assert "fixture-sensitive-text-never-send" not in json.dumps(fake_model["calls"])
    assert "fixture-external-text-never-send" not in json.dumps(fake_model["calls"])
    assert fake_model["authorization"] == "Bearer fake-model-key"
    assert os.environ["PROJECT_MODEL_TOKEN"] == "fake-model-key"


@pytest.mark.parametrize("name", ["../outside.txt", "/not-a-real-fixture.txt", "outside.txt"])
def test_scope_blocks_reads_and_writes_at_the_tool(repo, tmp_path, fake_model, model_file, name):
    fake_model["script"] = [("read_file", {"path": name}), ("write_file", {"path": name, "content": "blocked"}),
                            ("finish", {"summary": "No changes"})]
    with pytest.raises(ScopeError):
        propose(repo / "calc.py", tmp_path / "proposal", task="Inspect", model_file=model_file)
    assert "error" in json.loads(fake_model["calls"][1]["messages"][-1]["content"])
    assert "error" in json.loads(fake_model["calls"][2]["messages"][-1]["content"])


def test_fix_tests_requires_output(tmp_path):
    with pytest.raises(ValueError, match="requires --test-output"):
        propose(tmp_path, tmp_path / "proposal", fix_tests=True)


@pytest.mark.parametrize("kind", ["parent-link", "fifo"])
def test_apply_refuses_nonregular_targets_or_symlink_parents(tmp_path, fake_model, model_file, kind):
    project = tmp_path / "project"
    (project / "nested").mkdir(parents=True)
    target = project / "nested/code.txt"
    target.write_text("original")
    fake_model["script"] = [("write_file", {"path": "nested/code.txt", "content": "fixed"}),
                            ("finish", {"summary": "Fix"})]
    out = tmp_path / "proposal"
    propose(project, out, task="Fix", model_file=model_file)
    if kind == "parent-link":
        (project / "nested").rename(project / "original-dir")
        (project / "nested").symlink_to(project / "original-dir", target_is_directory=True)
    else:
        target.unlink()
        os.mkfifo(target)
    with pytest.raises(ValueError):
        apply_proposal(out)


@pytest.mark.parametrize("mode", ["run", "daemon", "swarm", "event", "notify"])
def test_existing_command_paths_strip_configured_and_standard_keys(repo, fake_model, monkeypatch, tmp_path, mode):
    set_keys(monkeypatch)
    cfg = config(repo, fake_model)
    cfg["model"]["api_key_env"] = "PROJECT_MODEL_TOKEN"
    fake_model["script"] = [("run_command", {"command": secret_check()}), ("finish", {"summary": "Checked"})]
    params = {"task": "Inspect", "check_command": secret_check()}
    if mode == "swarm":
        result = swarm(cfg, "demo", [("custom", params)], 1, tmp_path / "logs")
        records = result["workers"]
    elif mode == "daemon":
        records = isolated_run(cfg, "custom", "demo", params, True, tmp_path / "logs")
    elif mode == "event":
        params["event_command"] = secret_check()
        records = run(cfg, "datadog-alert", "demo", params, True, tmp_path / "logs")
    elif mode == "notify":
        output = tmp_path / "notifier.txt"
        params["notify_command"] = secret_check() + " > " + shlex.quote(str(output))
        records = run(cfg, "custom", "demo", params, False, tmp_path / "logs")
        assert "keys absent" in output.read_text()
    else:
        records = run(cfg, "custom", "demo", params, True, tmp_path / "logs")
    assert records[0]["check"]["returncode"] == 0
    result = json.loads(fake_model["calls"][1]["messages"][-1]["content"])
    assert result["returncode"] == 0


def test_direct_command_strips_standard_keys(tmp_path, monkeypatch):
    set_keys(monkeypatch)
    assert command(tmp_path, secret_check(), api_key_env="PROJECT_MODEL_TOKEN")["returncode"] == 0


def test_daemon_startup_git_commands_strip_configured_key(repo, fake_model, monkeypatch):
    set_keys(monkeypatch)
    cfg = config(repo, fake_model)
    cfg["model"]["api_key_env"] = "PROJECT_MODEL_TOKEN"
    real_run = subprocess.run
    commands = []

    def checked_run(args, **kwargs):
        commands.append(args)
        assert "PROJECT_MODEL_TOKEN" not in kwargs["env"]
        return real_run(args, **kwargs)

    def stop(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess, "run", checked_run)
    monkeypatch.setattr(runner.time, "sleep", stop)
    with pytest.raises(KeyboardInterrupt):
        runner.daemon(cfg, repo / "logs")
    assert commands == [["git", "rev-parse", "--git-common-dir"]]

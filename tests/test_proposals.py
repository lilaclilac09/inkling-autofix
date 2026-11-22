import json
import os
import shlex
import subprocess
import sys
import tempfile

import pytest
import yaml

from autofix import cli, runner
from autofix.agent import command
from autofix.gitops import git
from autofix.proposals import ScopeError, apply_proposal, digest, proposal_model, propose
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


def test_propose_folder_and_apply_add_modify_delete_binary(tmp_path, fake_model, model_file):
    project = tmp_path / "project"
    project.mkdir()
    (project / "code.py").write_text("broken\n")
    (project / "obsolete.txt").write_text("obsolete\n")
    (project / "node_modules").mkdir()
    (project / "node_modules" / "cache.txt").write_text("leave alone")
    fake_model["script"] = [
        ("write_file", {"path": "code.py", "content": "fixed\n"}),
        ("write_file", {"path": "new.txt", "content": "new\n"}),
        ("run_command", {"command": "rm obsolete.txt"}),
        ("run_command", {"command": f"{shlex.quote(sys.executable)} -c " + shlex.quote(
            "from pathlib import Path; Path('binary.dat').write_bytes(bytes([0, 255]))")}),
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
    assert "-broken" in diff and "+fixed" in diff and "GIT binary patch" in diff
    assert (out / "summary.txt").read_text() == "Fix code and remove obsolete data\n"
    assert apply_proposal(out)["applied"] == 4
    assert (project / "code.py").read_text() == "fixed\n"
    assert (project / "new.txt").read_text() == "new\n"
    assert (project / "binary.dat").read_bytes() == bytes([0, 255])
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


@pytest.mark.parametrize("passes", [True, False])
def test_fix_tests_check_in_copy(repo, tmp_path, fake_model, model_file, passes):
    fake_model["script"] = ([("replace_in_file", {"path": "calc.py", "old": "return a - b",
                                                  "new": "return a + b"})] if passes else [])
    fake_model["script"].append(("finish", {"summary": "Fix tests"}))
    out = tmp_path / "proposal"
    args = {"fix_tests": True, "test_command": f"{shlex.quote(sys.executable)} -B -m pytest -q",
            "model_file": model_file}
    if passes:
        propose(repo, out, **args)
        assert (out / "manifest.json").exists()
    else:
        with pytest.raises(RuntimeError, match="Proposal check failed"):
            propose(repo, out, **args)
        assert not (out / "manifest.json").exists()
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


def test_out_of_scope_takes_priority_over_failed_check(repo, tmp_path, fake_model, model_file):
    fake_model["script"] = [("write_file", {"path": "test_calc.py", "content": "outside scope"}),
                            ("finish", {"summary": "Attempt"})]
    out = tmp_path / "proposal"
    with pytest.raises(ScopeError, match="test_calc.py"):
        propose(repo / "calc.py", out, fix_tests=True, test_command="exit 1", model_file=model_file)
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


def test_proposal_agent_and_check_cannot_see_keys(tmp_path, fake_model, model_file, monkeypatch):
    set_keys(monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    fake_model["script"] = [("run_command", {"command": secret_check()}), ("finish", {"summary": "Checked"})]
    propose(project, tmp_path / "proposal", fix_tests=True, test_command=secret_check(), model_file=model_file)
    result = json.loads(fake_model["calls"][1]["messages"][-1]["content"])
    assert result["returncode"] == 0 and "keys absent" in result["output"]
    assert fake_model["authorization"] == "Bearer fake-model-key"
    assert os.environ["PROJECT_MODEL_TOKEN"] == "fake-model-key"


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

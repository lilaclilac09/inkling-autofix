import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from autofix.agent import Agent
from autofix.gitops import git
from autofix.runner import run
from autofix import runner
from autofix.gitops import complete, prepare


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (root / "test_calc.py").write_text("from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n")
    git(root, "add", ".gitignore", "calc.py", "test_calc.py")
    git(root, "commit", "-m", "initial")
    return root


@pytest.fixture
def fake_model():
    state = {"calls": [], "script": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["calls"].append(request)
            state["authorization"] = self.headers.get("Authorization")
            tool = state["handler"](request) if "handler" in state else state["script"].pop(0)
            message = {"role": "assistant", "content": "No tools"}
            if isinstance(tool, str):
                message = {"role": "assistant", "content": tool}
            elif tool:
                name, args = tool
                message = {"role": "assistant", "content": None, "tool_calls": [
                    {"id": f"call_{len(state['calls'])}", "type": "function",
                     "function": {"name": name, "arguments": json.dumps(args)}}]}
            body = json.dumps({"id": "fake", "object": "chat.completion", "created": 0,
                               "model": "fake", "choices": [{"index": 0, "message": message,
                               "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 2,
                               "completion_tokens": 3, "total_tokens": 5}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["model"] = {"base_url": f"http://127.0.0.1:{server.server_port}/v1",
                      "model": "fake", "max_steps": 8}
    yield state
    server.shutdown()
    thread.join()
    server.server_close()


def config(repo, fake_model):
    return {"model": fake_model["model"], "repos": [{"name": "demo", "path": str(repo),
            "base_branch": "main"}]}


def test_full_fix(repo, fake_model, monkeypatch, tmp_path):
    monkeypatch.setenv("INKLING_API_KEY", "fake-not-a-secret")
    check = f"{sys.executable} -B -m pytest -q"
    failed = subprocess.run(check, shell=True, cwd=repo, capture_output=True)
    assert failed.returncode != 0
    fake_model["script"] = [
        ("read_file", {"path": "calc.py"}),
        ("replace_in_file", {"path": "calc.py", "old": "return a - b", "new": "return a + b"}),
        ("finish", {"summary": "Fix addition"}),
    ]
    records = run(config(repo, fake_model), "fix_failing_tests", "demo",
                  {"test_command": check}, True, tmp_path / "logs")
    assert "return a + b" in (repo / "calc.py").read_text()
    assert git(repo, "branch", "--show-current").startswith("autofix/fix_failing_tests-")
    assert git(repo, "rev-list", "--count", "HEAD") == "2"
    assert git(repo, "show", "main:calc.py").endswith("return a - b")
    assert records[0]["check"]["returncode"] == 0
    assert "1 passed" in records[0]["check"]["output"]
    assert not git(repo, "status", "--porcelain")
    logged = json.loads((tmp_path / "logs/runs.jsonl").read_text())
    assert logged["usage"]["total_tokens"] == 15
    assert fake_model["calls"][1]["messages"][-1]["role"] == "tool"
    assert "finish summary in English" in fake_model["calls"][0]["messages"][0]["content"]


@pytest.mark.parametrize("mode", ["empty", "failed_check", "no_tools", "max_steps"])
def test_no_unverified_commit(repo, fake_model, monkeypatch, tmp_path, mode):
    monkeypatch.setenv("INKLING_API_KEY", "fake")
    fake_model["script"] = [("finish", {"summary": "Nothing changed"})]
    params = {"check_command": f'{sys.executable} -c "pass"'}
    if mode == "failed_check":
        params = {"check_command": f'{sys.executable} -c "exit(1)"'}
    elif mode == "no_tools":
        fake_model["script"] = [None]
    elif mode == "max_steps":
        fake_model["model"]["max_steps"] = 1
        fake_model["script"] = [("read_file", {"path": "calc.py"})]
    if mode == "empty":
        assert run(config(repo, fake_model), "fix_failing_tests", "demo", params,
                   True, tmp_path / "logs")[0]["changed"] is False
    else:
        with pytest.raises(RuntimeError, match={"failed_check": "Check failed",
                           "no_tools": "tool calls", "max_steps": "max_steps"}[mode]):
            run(config(repo, fake_model), "fix_failing_tests", "demo", params,
                True, tmp_path / "logs")
    assert git(repo, "rev-list", "--count", "HEAD") == "1"


def test_path_guards_and_tools(repo, tmp_path):
    agent = Agent(repo, {"base_url": "http://localhost/v1"}, "fake")
    (repo / "escape").symlink_to(tmp_path)
    for path in ["../outside", str(tmp_path), "escape/outside", ".git/config"]:
        with pytest.raises(ValueError, match="Path must"):
            agent.execute("read_file", {"path": path})
    agent.execute("write_file", {"path": "new/file.txt", "content": "hello"})
    assert agent.execute("read_file", {"path": "new/file.txt"}) == "hello"
    assert "new/file.txt" in agent.execute("list_files", {"path": "new"})
    with pytest.raises(ValueError, match="exactly once"):
        agent.execute("replace_in_file", {"path": "calc.py", "old": "missing", "new": ""})


def test_dirty_repo_rejected(repo, fake_model, monkeypatch, tmp_path):
    monkeypatch.setenv("INKLING_API_KEY", "fake")
    (repo / "calc.py").write_text("dirty")
    with pytest.raises(RuntimeError, match="clean"):
        run(config(repo, fake_model), "fix_failing_tests", "demo", log_dir=tmp_path / "logs")
    assert git(repo, "branch", "--show-current") == "main"
    assert not fake_model["calls"]


@pytest.mark.parametrize("template", ["fix_github_issues", "fix_ci_failure"])
@pytest.mark.parametrize("empty", [True, False])
def test_github_contexts(repo, fake_model, monkeypatch, tmp_path, template, empty):
    monkeypatch.setenv("INKLING_API_KEY", "fake")
    calls = []

    def fake_gh(root, *args):
        calls.append(args)
        if args[:2] == ("issue", "list"):
            assert args[args.index("--limit") + 1] == "200"
            return json.dumps([] if empty else [{"number": 1, "title": "Bug", "body": "Fix me"},
                                                {"number": 2, "title": "Bug 2", "body": "Fix me too"}])
        if args[:2] == ("run", "list"):
            return json.dumps([] if empty else [{"databaseId": 42, "headBranch": "main",
                                                 "headSha": git(repo, "rev-parse", "main")}])
        if args == ("api", "repos/owner/repo/actions/runs/42"):
            return json.dumps({"head_repository": {"full_name": "owner/repo"}, "head_branch": "main",
                               "head_sha": git(repo, "rev-parse", "main")})
        if args == ("api", "repos/owner/repo/branches/main"):
            return json.dumps({"commit": {"sha": git(repo, "rev-parse", "main")}})
        assert args[:3] == ("run", "view", "42")
        assert "--log-failed" in args
        return "CI assertion failed"

    monkeypatch.setattr(runner, "gh", fake_gh)
    cfg = config(repo, fake_model)
    cfg["repos"][0]["github"] = "owner/repo"
    count = 0 if empty else (2 if template == "fix_github_issues" else 1)
    fake_model["script"] = [("finish", {"summary": "No change"})] * count
    records = run(cfg, template, "demo", {"check_command": f'{sys.executable} -c "pass"'},
                  True, tmp_path / "logs")
    assert len(records) == count
    assert len({r["branch"] for r in records}) == count
    assert all(r["check"]["returncode"] == 0 for r in records)
    if count:
        prompt = fake_model["calls"][0]["messages"][1]["content"]
        assert ("Fix me" if template == "fix_github_issues" else "CI assertion failed") in prompt
    assert all(c[:2] != ("issue", "comment") for c in calls)


def test_check_cannot_switch_to_base(repo):
    branch = prepare(repo, "custom", "main")
    with pytest.raises(RuntimeError, match="Check changed branch"):
        complete(repo, branch, "main", "Do not commit", "git switch main", None, True)
    assert git(repo, "rev-list", "--count", "main") == "1"


def test_empty_finish_summary_uses_default(repo, fake_model):
    fake_model["script"] = [("finish", {"summary": "  \n"})]
    summary = Agent(repo, fake_model["model"], "fake").run("Do nothing")
    assert summary == "Complete automated task"
    branch = prepare(repo, "custom", "main")
    (repo / "note.txt").write_text("done")
    complete(repo, branch, "main", "", None, None, True)
    assert git(repo, "show", "-s", "--format=%s") == "autofix: Complete automated task"


def test_logs_inside_repo_do_not_dirty_next_run(repo, fake_model, monkeypatch):
    monkeypatch.setenv("INKLING_API_KEY", "fake")
    fake_model["script"] = [("finish", {"summary": "No changes"})] * 2
    for _ in range(2):
        run(config(repo, fake_model), "custom", "demo", {"task": "Inspect only"},
            True, repo / "logs")
    assert not (repo / "logs").exists()
    assert (repo / ".git/autofix-logs/runs.jsonl").exists()
    assert git(repo, "status", "--porcelain") == ""


def test_daemon_schedule_and_error_logging(monkeypatch, tmp_path):
    clock = [0]
    invocations = []
    cfg = {"repos": [], "automations": [{"name": "job", "template": "custom", "repo": "demo",
                           "schedule_minutes": 1, "enabled": True, "task": "fix"},
                          {"name": "off", "enabled": False}]}

    def fake_run(*args, **kwargs):
        invocations.append((args, kwargs))
        raise RuntimeError("Expected model failure")

    def advance(_):
        clock[0] += 30
        if clock[0] > 60:
            raise KeyboardInterrupt

    monkeypatch.setattr(runner, "isolated_run", fake_run)
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runner.time, "sleep", advance)
    with pytest.raises(KeyboardInterrupt):
        runner.daemon(cfg, tmp_path / "logs")
    assert len(invocations) == 2
    assert invocations[0][0][3] == {"task": "fix"}
    records = [json.loads(line) for line in (tmp_path / "logs/daemon.jsonl").read_text().splitlines()]
    assert len(records) == 2
    assert all(r["error"] == "Expected model failure" for r in records)

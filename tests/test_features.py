import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from autofix.agent import Agent, parse_json_calls
from autofix.events import command_context
from autofix.gitops import git
from autofix.providers import TINKER_BASE_URL, model_config
from autofix.review import findings, review
from autofix.runner import isolated_run, run, templates
from autofix.swarm import SwarmFailure, swarm
from autofix.workspaces import repo_lock
from test_runner import config, fake_model, repo


def test_tinker_defaults_and_overrides():
    cfg = model_config({"provider": "tinker", "model": "tinker://run/sampler_weights/1"})
    assert cfg["base_url"] == TINKER_BASE_URL
    assert cfg["api_key_env"] == "TINKER_API_KEY"
    assert cfg["tool_mode"] == "native"
    assert model_config({**cfg, "base_url": "http://local", "tool_mode": "json"})["base_url"] == "http://local"
    with pytest.raises(ValueError):
        model_config({"tool_mode": "invalid"})


def test_keyshield_defaults(monkeypatch):
    monkeypatch.delenv("KS_BASE", raising=False)
    with pytest.raises(ValueError, match="KS_BASE"):
        model_config({"provider": "keyshield", "model": "m"})
    cfg = model_config({"provider": "keyshield", "keyshield_base": "https://ks.example/",
                        "model": "thinkingmachines/Inkling:peft:262144"})
    assert cfg["base_url"] == "https://ks.example/vproxy/tinker"
    assert cfg["api_key_env"] == "KS_TOKEN"
    monkeypatch.setenv("KS_BASE", "http://127.0.0.1:8787/")
    assert model_config({"provider": "keyshield", "keyshield_upstream": "openai", "model": "m"})[
        "base_url"] == "http://127.0.0.1:8787/vproxy/openai/v1"
    with pytest.raises(ValueError, match="keyshield_path"):
        model_config({"provider": "keyshield", "keyshield_upstream": "custom", "model": "m"})
    with pytest.raises(ValueError, match="keyshield_base"):
        model_config({"provider": "keyshield", "base_url": "http://x", "model": "m"})


def test_keyshield_run_uses_vproxy_and_session_token(repo, fake_model, monkeypatch, tmp_path):
    monkeypatch.setenv("KS_TOKEN", "ks-proxy-token")
    monkeypatch.delenv("TINKER_API_KEY", raising=False)
    base = fake_model["model"].pop("base_url").removesuffix("/v1")
    fake_model["model"].update(provider="keyshield", keyshield_base=base)
    fake_model["script"] = [("finish", {"summary": "Nothing to change"})]
    run(config(repo, fake_model), "custom", "demo", {"task": "Inspect"}, True, tmp_path / "logs")
    assert fake_model["path"] == "/vproxy/tinker/chat/completions"
    assert fake_model["authorization"] == "Bearer ks-proxy-token"


@pytest.mark.parametrize("base", ["http://ks.example", "https://", "ftp://ks.example",
                                  "https://user:pw@ks.example", "http://127.0.0.1.ks.example"])
def test_keyshield_base_requires_https_or_loopback(base):
    with pytest.raises(ValueError, match="https URL or http on loopback"):
        model_config({"provider": "keyshield", "keyshield_base": base, "model": "m"})


@pytest.mark.parametrize("base", ["https://ks.example", "http://127.0.0.1:8787", "http://localhost:8787",
                                  "http://[::1]:8787"])
def test_keyshield_base_accepts_https_and_loopback(base):
    assert model_config({"provider": "keyshield", "keyshield_base": base, "model": "m"})[
        "base_url"] == f"{base}/vproxy/tinker"


@pytest.mark.parametrize("key_env", [None, "MY_KS_TOKEN"])
def test_keyshield_token_hidden_from_agent_and_check(repo, fake_model, monkeypatch, tmp_path, key_env):
    names = sorted({"KS_TOKEN", key_env or "KS_TOKEN"})
    for name in names:
        monkeypatch.setenv(name, "ks-proxy-token")
    probe = f"{sys.executable} -c \"import os; assert not any(n in os.environ for n in {names!r}); print('absent')\""
    base = fake_model["model"].pop("base_url").removesuffix("/v1")
    fake_model["model"].update(provider="keyshield", keyshield_base=base)
    if key_env:
        fake_model["model"]["api_key_env"] = key_env
    fake_model["script"] = [("run_command", {"command": probe}), ("finish", {"summary": "Checked"})]
    records = run(config(repo, fake_model), "custom", "demo",
                  {"task": "Inspect", "check_command": probe}, True, tmp_path / "logs")
    assert records[0]["check"]["returncode"] == 0 and "absent" in records[0]["check"]["output"]
    result = json.loads(fake_model["calls"][1]["messages"][-1]["content"])
    assert result["returncode"] == 0 and "absent" in result["output"]
    assert fake_model["authorization"] == "Bearer ks-proxy-token"


def test_json_tool_loop(repo, fake_model, monkeypatch, tmp_path):
    monkeypatch.setenv("TINKER_API_KEY", "fake-key")
    fake_model["model"].update(provider="tinker", tool_mode="json")
    fake_model["script"] = [
        json.dumps({"tool_calls": [{"name": "read_file", "arguments": {"path": "calc.py"}}]}),
        '```json\n' + json.dumps({"tool_calls": [{"name": "replace_in_file", "arguments": {
            "path": "calc.py", "old": "a - b", "new": "a + b"}}]}) + '\n```',
        json.dumps({"tool_calls": [{"name": "finish", "arguments": {"summary": "Fix addition"}}]}),
    ]
    result = run(config(repo, fake_model), "fix_failing_tests", "demo",
                 {"test_command": f"{sys.executable} -B -m pytest -q"}, True, tmp_path / "logs")
    assert result[0]["changed"]
    assert result[0]["check"]["returncode"] == 0
    assert all("tools" not in request and "response_format" not in request for request in fake_model["calls"])
    assert fake_model["calls"][1]["messages"][-1]["role"] == "user"
    assert fake_model["authorization"] == "Bearer fake-key"


@pytest.mark.parametrize("text", ["not json", '{}', '{"tool_calls":[]}',
                                '{"tool_calls":[{"name":"run_command","arguments":{}}]}'])
def test_malformed_json_fails_closed(repo, fake_model, text):
    fake_model["model"]["tool_mode"] = "json"
    fake_model["script"] = [text]
    with pytest.raises(RuntimeError, match="Invalid JSON tool response"):
        Agent(repo, fake_model["model"], "fake", read_only=True).run("Inspect")
    assert git(repo, "status", "--porcelain") == ""


def test_full_catalog():
    catalog = templates()
    expected = {
        "monitor-bugs-slack", "customer-support-triage", "pylon-cs-bug-triage",
        "pylon-engineering-escalation", "datadog-alert", "sentry-remediation",
        "daily-error-report", "capacity-planning", "auto-fix-ci", "issue-fix",
        "dependency-updates", "weekly-changelog", "stale-pr-cleanup", "merge-watch",
        "dead-code-watch", "security-dependency-vulns", "security-secret-scan",
        "security-code-patterns", "security-owasp-hardening", "cloudflare-security-audit",
        "notion-weekly-status", "asana-sprint-report", "backlog-cleanup-linear",
    }
    assert expected <= catalog.keys()
    assert len(catalog) == 29
    for name in expected:
        template = catalog[name]
        assert template["name"] == name
        assert template["category"] and template["description"] and template["notes"]
        template["prompt"].format(task="test", context="event", test_command="true")
        if template["trigger"]["type"] == "command":
            assert "AUTOFIX_EVENT_COMMAND" in template["required_env"]


def test_command_event_empty_success_error(repo, monkeypatch):
    monkeypatch.delenv("AUTOFIX_EVENT_COMMAND", raising=False)
    template = templates()["datadog-alert"]
    with pytest.raises(ValueError, match="requires event_command"):
        command_context(repo, template, {})
    assert command_context(repo, template, {"event_command": "printf ''"}) == []
    assert command_context(repo, template, {"event_command": "printf alert"}) == [(None, "alert")]
    with pytest.raises(RuntimeError, match="Event command failed"):
        command_context(repo, template, {"event_command": "exit 1"})


def test_event_adapter_stderr_is_diagnostic_not_an_event(repo, tmp_path):
    template = templates()["datadog-alert"]
    params = {"event_command": "printf 'no new events' >&2"}
    assert command_context(repo, template, params, tmp_path) == []
    assert "no new events" in (tmp_path / "event_diagnostics.jsonl").read_text()
    params["event_command"] = "printf event; printf warning >&2"
    assert command_context(repo, template, params, tmp_path) == [(None, "event")]


def test_lock_rejects_same_repository(repo):
    with repo_lock(repo):
        with pytest.raises(RuntimeError, match="locked"):
            with repo_lock(repo):
                pass


def test_daemon_worktree_keeps_original_clean(repo, fake_model, monkeypatch):
    monkeypatch.setenv("INKLING_API_KEY", "fake")
    fake_model["script"] = [("finish", {"summary": "No changes"})] * 2
    for _ in range(2):
        results = isolated_run(config(repo, fake_model), "custom", "demo", {"task": "Inspect"},
                               True, repo / "logs")
        assert Path(results[0]["worktree"]) != repo
    assert git(repo, "branch", "--show-current") == "main"
    assert git(repo, "status", "--porcelain") == ""
    assert not (repo / "logs").exists()


@pytest.mark.parametrize("resolve,fail_check", [(True, False), (False, False), (True, True)])
def test_swarm_conflict_resolved_or_stopped(repo, fake_model, monkeypatch, resolve, fail_check):
    monkeypatch.setenv("INKLING_API_KEY", "fake")
    barrier = threading.Barrier(2)
    resolver_calls = []

    def respond(request):
        prompt = request["messages"][1]["content"]
        turns = sum(m["role"] == "assistant" for m in request["messages"])
        if "reviewer" in prompt or "aggregator" in prompt:
            return "finish", {"summary": json.dumps({"summary": "Approved", "approved": True, "findings": []})}
        if "Resolve this swarm" in prompt:
            resolver_calls.append(request)
            if not resolve:
                return "finish", {"summary": "Unable to resolve"}
            return (("write_file", {"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"})
                    if turns == 0 else ("finish", {"summary": "Resolve addition conflict"}))
        if turns == 0:
            barrier.wait(timeout=10)
            expr = "a + b" if "left attempt" in prompt else "b + a"
            return "write_file", {"path": "calc.py", "content": f"def add(a, b):\n    return {expr}\n"}
        return "finish", {"summary": "Fix addition"}

    fake_model["handler"] = respond
    check = f"{sys.executable} -B -m pytest -q"
    jobs = [("custom", {"task": task, "check_command": check}) for task in ["left attempt", "right attempt"]]
    if fail_check:
        from autofix import swarm as swarm_module

        checks = []

        def check_resolution(root, check):
            checks.append(check)
            return {"returncode": int(len(checks) == 2), "output": "Integration check"}

        monkeypatch.setattr(swarm_module, "command", check_resolution)
    if resolve and not fail_check:
        result = swarm(config(repo, fake_model), "demo", jobs, 2, repo / "logs")
    else:
        with pytest.raises(SwarmFailure) as failure:
            swarm(config(repo, fake_model), "demo", jobs, 2, repo / "logs")
        result = failure.value.result
        assert result["status"] == "failed" and not result["published"]
        assert Path(result["report"]).exists()
        from autofix import cli

        monkeypatch.setattr(cli, "load_config", lambda _: config(repo, fake_model))

        def stopped(*args, **kwargs):
            raise failure.value

        monkeypatch.setattr(cli, "swarm", stopped)
        monkeypatch.setattr(sys, "argv", ["autofix", "swarm", "custom", "--repo", "demo"])
        with pytest.raises(SystemExit) as exit_status:
            cli.main()
        assert exit_status.value.code == 1
    assert len(result["merged"]) == (2 if resolve and not fail_check else 1)
    assert len(result["failures"]) == (0 if resolve and not fail_check else 1)
    assert resolver_calls
    assert any("calc.py" in files for files in result["overlaps"].values())
    assert any(item["result"]["returncode"] != 0 for item in result["checks"]) == fail_check
    assert result["branch"].startswith("autofix/swarm-")
    assert (Path(result["worktree"]) / "calc.py").read_text().endswith("return a + b\n")
    assert git(repo, "branch", "--show-current") == "main"
    assert git(repo, "status", "--porcelain") == ""


def test_swarm_empty_worker_results(repo, fake_model, monkeypatch):
    monkeypatch.setenv("INKLING_API_KEY", "fake")
    fake_model["script"] = [("finish", {"summary": "No changes"})]
    result = swarm(config(repo, fake_model), "demo", [("custom", {"task": "Inspect"})],
                   1, repo / "logs")
    assert not result["merged"] and not result["failures"]
    assert git(repo, "status", "--porcelain") == ""


def test_swarm_one_reviewer_rejects_stops_without_publishing(repo, fake_model, monkeypatch, tmp_path):
    monkeypatch.setenv("INKLING_API_KEY", "fake")
    from autofix import swarm as swarm_module

    def integration_check(root, check):
        (root / "generated.txt").write_text("Check-generated file must be reviewed")
        return {"returncode": 0, "output": "Checked"}

    monkeypatch.setattr(swarm_module, "command", integration_check)

    def respond(request):
        prompt = request["messages"][1]["content"]
        if "reviewer" in prompt or "aggregator" in prompt:
            if "reviewer" in prompt:
                assert "generated.txt" in prompt
            approved = "read-only security reviewer" not in prompt
            return "finish", {"summary": json.dumps({"summary": "Review", "approved": approved, "findings": []})}
        turns = sum(m["role"] == "assistant" for m in request["messages"])
        return (("write_file", {"path": "note.txt", "content": "a note"})
                if turns == 0 else ("finish", {"summary": "Add note"}))

    fake_model["handler"] = respond
    with pytest.raises(SwarmFailure, match="unanimous") as failure:
        swarm(config(repo, fake_model), "demo", [("custom", {"task": "Add note", "check_command": "true"})],
              1, tmp_path / "logs")
    result = failure.value.result
    assert not result["published"] and not result["merged"]
    assert not result["reviews"][0]["approved"]
    assert Path(result["report"]).exists()
    assert git(repo, "branch", "--show-current") == "main"
    assert git(repo, "status", "--porcelain") == ""


def test_review_invalid_aggregator_fails_closed(repo, fake_model, monkeypatch):
    monkeypatch.setenv("INKLING_API_KEY", "fake")
    git(repo, "switch", "-c", "fix")
    (repo / "note.txt").write_text("a note")
    git(repo, "add", "note.txt")
    git(repo, "commit", "-m", "note")

    def respond(request):
        prompt = request["messages"][1]["content"]
        return "finish", {"summary": "malformed" if "aggregator" in prompt else json.dumps({
            "summary": "Looks good", "approved": True, "findings": []})}

    fake_model["handler"] = respond
    result = review(config(repo, fake_model), "demo", branch="fix", log_dir=repo / "logs")
    assert not result["approved"]
    assert "fails closed" in result["error"]
    assert Path(result["report"]).exists()
    assert git(repo, "status", "--porcelain") == ""


def test_ci_poll_excludes_automation_prs(repo, fake_model, monkeypatch, tmp_path):
    from autofix import runner

    monkeypatch.setenv("INKLING_API_KEY", "fake")
    cfg = config(repo, fake_model)
    cfg["repos"][0]["github"] = "owner/repo"

    def fake_gh(root, *args):
        if args[:2] == ("run", "list"):
            return json.dumps([{"databaseId": 1, "headBranch": "autofix/fix"}])
        assert args[:2] == ("pr", "list")
        return json.dumps([{"number": 1, "headRefName": "autofix/fix", "author": {"login": "human"},
                            "isCrossRepository": False, "headRepository": {"id": "repo-id", "name": "repo"},
                            "headRepositoryOwner": {"login": "owner"}}])

    monkeypatch.setattr(runner, "gh", fake_gh)
    assert run(cfg, "auto-fix-ci", "demo", dry_run=True, log_dir=tmp_path / "logs") == []
    assert not fake_model["calls"]


def test_review_aggregation_preserves_blockers(repo, fake_model, monkeypatch):
    monkeypatch.setenv("INKLING_API_KEY", "fake")
    git(repo, "switch", "-c", "fix")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    git(repo, "add", "calc.py")
    git(repo, "commit", "-m", "fix")

    def respond(request):
        names = {t["function"]["name"] for t in request["tools"]}
        assert names == {"read_file", "list_files", "finish"}
        prompt = request["messages"][1]["content"]
        blocking = "read-only security reviewer" in prompt
        return "finish", {"summary": json.dumps({"summary": "Reviewed", "approved": not blocking, "findings": [
            {"blocking": True, "path": "calc.py", "line": 2, "reason": "Example finding"}
        ] if blocking else []})}

    fake_model["handler"] = respond
    result = review(config(repo, fake_model), "demo", branch="fix", log_dir=repo / "logs")
    assert len(result["reviewers"]) == 3
    assert result["blocking"] is True
    assert len(result["usage"]) == 4
    assert Path(result["report"]).read_text().startswith("# Autofix multi-agent review")
    assert git(repo, "status", "--porcelain") == ""
    agent = Agent(repo, fake_model["model"], "fake", read_only=True)
    with pytest.raises(ValueError, match="read-only"):
        agent.execute("run_command", {"command": "touch bad"})


def test_review_empty_diff_and_invalid_results(repo, fake_model, monkeypatch):
    monkeypatch.setenv("INKLING_API_KEY", "fake")
    result = review(config(repo, fake_model), "demo", log_dir=repo / "logs")
    assert not result["blocking"] and not result["findings"]
    assert not fake_model["calls"]
    with pytest.raises(RuntimeError, match="fails closed"):
        findings("not JSON")


def test_blocking_review_prevents_push(repo, fake_model, monkeypatch):
    monkeypatch.setenv("INKLING_API_KEY", "fake")
    cfg = config(repo, fake_model)
    cfg["repos"][0]["github"] = "owner/repo"

    def respond(request):
        prompt = request["messages"][1]["content"]
        if "reviewer" in prompt or "aggregator" in prompt:
            return "finish", {"summary": json.dumps({"summary": "Block", "approved": False, "findings": [
                {"blocking": True, "path": "note.txt", "line": 1, "reason": "Needs review"}]})}
        turns = sum(m["role"] == "assistant" for m in request["messages"])
        return (("write_file", {"path": "note.txt", "content": "a note"})
                if turns == 0 else ("finish", {"summary": "Add note"}))

    fake_model["handler"] = respond
    result = run(cfg, "custom", "demo", {"task": "Add note"}, False, repo / "logs")[0]
    assert result["publication_blocked"] and not result["pr"]
    assert git(repo, "rev-list", "--count", "HEAD") == "2"
    assert git(repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("verdict", ["reject", "error", "exception", "approve"])
def test_publication_requires_every_reviewer_to_approve(repo, fake_model, monkeypatch, verdict):
    from autofix import gitops

    monkeypatch.setenv("INKLING_API_KEY", "fake")
    cfg = config(repo, fake_model)
    cfg["repos"][0]["github"] = "owner/repo"
    published = []
    real_run = subprocess.run

    def intercept(args, **kwargs):
        if isinstance(args, list) and args[:2] == ["git", "push"]:
            published.append(args)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if isinstance(args, list) and args[:3] == ["gh", "pr", "create"]:
            published.append(args)
            return subprocess.CompletedProcess(args, 0, stdout="https://example.invalid/pr/1", stderr="")
        return real_run(args, **kwargs)

    monkeypatch.setattr(gitops.shutil, "which", lambda _: "/fake/gh")
    monkeypatch.setattr(gitops.subprocess, "run", intercept)

    def respond(request):
        prompt = request["messages"][1]["content"]
        if "reviewer" in prompt or "aggregator" in prompt:
            security = "read-only security reviewer" in prompt
            if security and verdict == "error":
                return "finish", {"summary": "invalid review JSON"}
            if security and verdict == "exception":
                return None
            approved = not (security and verdict == "reject")
            return "finish", {"summary": json.dumps({"summary": "Review result", "approved": approved,
                                                       "findings": []})}
        turns = sum(m["role"] == "assistant" for m in request["messages"])
        return (("write_file", {"path": "note.txt", "content": "a note"})
                if turns == 0 else ("finish", {"summary": "Add note"}))

    fake_model["handler"] = respond
    result = run(cfg, "custom", "demo", {"task": "Add note"}, False, repo / "logs")[0]
    if verdict == "approve":
        assert result["review"]["approved"]
        assert len(published) == 2
        assert result["pr"] == "https://example.invalid/pr/1"
    else:
        assert result["publication_blocked"]
        assert not published
        assert Path(result["review"]["report"]).exists()
        assert not result["review"]["approved"]
    assert git(repo, "rev-list", "--count", "HEAD") == "2"

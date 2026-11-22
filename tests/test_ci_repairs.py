import json

import pytest

from autofix import runner
from autofix.gitops import git
from autofix.swarm import swarm
from test_runner import config, fake_model, repo


@pytest.mark.parametrize("template", ["fix_ci_failure", "auto-fix-ci"])
@pytest.mark.parametrize("advanced", [False, True])
def test_ci_fix_starts_at_failed_pr_commit(repo, fake_model, monkeypatch, tmp_path, template, advanced):
    monkeypatch.setenv("INKLING_API_KEY", "fake")
    git(repo, "switch", "-c", "feature")
    (repo / "pr_only.txt").write_text("broken")
    git(repo, "add", "pr_only.txt")
    git(repo, "commit", "-m", "PR-only change")
    sha = git(repo, "rev-parse", "HEAD")
    if advanced:
        (repo / "later.txt").write_text("Later branch change")
        git(repo, "add", "later.txt")
        git(repo, "commit", "-m", "Advance PR branch")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "main")
    cfg = config(repo, fake_model)
    cfg["repos"][0]["github"] = "owner/repo"

    def fake_gh(root, *args):
        if args[:2] == ("run", "list"):
            assert "headSha" in args[args.index("--json") + 1]
            return json.dumps([{"databaseId": 42, "headBranch": "feature", "headSha": sha}])
        if args[:2] == ("pr", "list"):
            assert "headRepository" in args[args.index("--json") + 1]
            return json.dumps([{"number": 8, "headRefName": "feature", "isCrossRepository": True,
                                "headRepository": {"nameWithOwner": "fork/repo"}, "author": {"login": "human"}},
                               {"number": 7, "headRefName": "feature", "isCrossRepository": False,
                                "headRepository": {"nameWithOwner": "owner/repo"}, "author": {"login": "human"}}])
        if args == ("api", "repos/owner/repo/actions/runs/42"):
            return json.dumps({"head_repository": {"full_name": "owner/repo"},
                               "head_sha": sha, "head_branch": "feature"})
        if args == ("api", "repos/owner/repo/branches/feature"):
            return json.dumps({"commit": {"sha": head}})
        if args == ("api", f"repos/owner/repo/compare/{sha}...{head}"):
            assert advanced
            return json.dumps({"status": "ahead"})
        assert args[:3] == ("run", "view", "42")
        return "pr_only.txt is broken"

    monkeypatch.setattr(runner, "gh", fake_gh)
    fake_model["script"] = [
        ("read_file", {"path": "pr_only.txt"}),
        ("replace_in_file", {"path": "pr_only.txt", "old": "broken", "new": "fixed"}),
        ("finish", {"summary": "Fix PR regression"}),
    ]
    result = runner.run(cfg, template, "demo", {"check_command": "true"},
                        True, tmp_path / "logs")[0]
    assert result["start_ref"] == sha
    assert result["publish_base"] == "feature"
    assert (repo / "pr_only.txt").read_text() == "fixed"
    assert git(repo, "rev-parse", "HEAD^") == sha
    assert git(repo, "show", "feature:pr_only.txt") == "broken"


@pytest.mark.parametrize("template", ["fix_ci_failure", "auto-fix-ci"])
@pytest.mark.parametrize("branch", ["main", "feature"])
@pytest.mark.parametrize("source", ["fork", "missing", "unrelated_commit"])
def test_ci_rejects_untrusted_source_before_checkout(repo, fake_model, monkeypatch, tmp_path,
                                                     template, branch, source):
    monkeypatch.setenv("INKLING_API_KEY", "fake")
    base = git(repo, "rev-parse", "main")
    git(repo, "branch", "feature")
    git(repo, "switch", "-c", "untrusted")
    (repo / "payload.txt").write_text("This commit must never be checked out by the runner")
    git(repo, "add", "payload.txt")
    git(repo, "commit", "-m", "Untrusted commit")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "main")
    cfg = config(repo, fake_model)
    cfg["repos"][0]["github"] = "owner/repo"
    calls = []

    def fake_gh(root, *args):
        calls.append(args)
        if args[:2] == ("run", "list"):
            return json.dumps([{"databaseId": 42, "headBranch": branch, "headSha": sha}])
        if args[:2] == ("pr", "list"):
            return json.dumps([{"number": 7, "headRefName": branch, "isCrossRepository": False,
                                "headRepository": {"nameWithOwner": "owner/repo"}, "author": {"login": "human"}}])
        if args == ("api", "repos/owner/repo/actions/runs/42"):
            full_name = "owner/repo" if source == "unrelated_commit" else "fork/repo"
            return json.dumps({"head_repository": None if source == "missing" else {"full_name": full_name},
                               "head_sha": sha, "head_branch": branch})
        if args == ("api", f"repos/owner/repo/branches/{branch}"):
            assert source == "unrelated_commit"
            return json.dumps({"commit": {"sha": base}})
        if args == ("api", f"repos/owner/repo/compare/{sha}...{base}"):
            return json.dumps({"status": "behind"})
        pytest.fail(f"Untrusted source must not reach logs or other API calls: {args}")

    def no_checkout(*args):
        pytest.fail("Untrusted CI source reached checkout")

    monkeypatch.setattr(runner, "gh", fake_gh)
    monkeypatch.setattr(runner, "prepare", no_checkout)
    assert runner.run(cfg, template, "demo", {"check_command": "touch check_was_run"},
                      True, tmp_path / "logs") == []
    assert not fake_model["calls"]
    assert not (repo / "check_was_run").exists()
    assert git(repo, "branch", "--show-current") == "main"
    assert git(repo, "rev-parse", "HEAD") == base
    assert git(repo, "status", "--porcelain") == ""


def test_swarm_ci_poll_uses_branch_name_not_snapshot(repo, fake_model, monkeypatch, tmp_path):
    monkeypatch.setenv("INKLING_API_KEY", "fake")
    cfg = config(repo, fake_model)
    cfg["repos"][0]["github"] = "owner/repo"
    calls = []

    def fake_gh(root, *args):
        calls.append(args)
        assert args[:2] == ("run", "list")
        assert args[args.index("--branch") + 1] == "main"
        return "[]"

    monkeypatch.setattr(runner, "gh", fake_gh)
    result = swarm(cfg, "demo", [("fix_ci_failure", {})], 1, tmp_path / "logs")
    assert calls and not result["workers"]


def test_issue_fix_uses_autofix_comment_command(repo, fake_model, monkeypatch, tmp_path):
    monkeypatch.setenv("INKLING_API_KEY", "fake")
    cfg = config(repo, fake_model)
    cfg["repos"][0]["github"] = "owner/repo"

    def fake_gh(root, *args):
        if args[:2] == ("issue", "list"):
            return json.dumps([{"number": 1, "title": "Bug", "body": "Investigate"}])
        assert args[:2] == ("issue", "view")
        return json.dumps({"comments": [{"body": "/autofix please investigate"}]})

    monkeypatch.setattr(runner, "gh", fake_gh)
    fake_model["script"] = [("finish", {"summary": "No changes"})]
    result = runner.run(cfg, "issue-fix", "demo", {"check_command": "true"},
                        True, tmp_path / "logs")
    assert len(result) == 1
    assert "/autofix please investigate" in fake_model["calls"][0]["messages"][1]["content"]

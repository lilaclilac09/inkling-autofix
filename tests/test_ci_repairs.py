import json

import pytest

from autofix import runner
from autofix.gitops import git
from autofix.swarm import swarm
from test_runner import config, fake_model, repo


@pytest.mark.parametrize("template", ["fix_ci_failure", "auto-fix-ci"])
def test_ci_fix_starts_at_failed_pr_commit(repo, fake_model, monkeypatch, tmp_path, template):
    monkeypatch.setenv("INKLING_API_KEY", "fake")
    git(repo, "switch", "-c", "feature")
    (repo / "pr_only.txt").write_text("broken")
    git(repo, "add", "pr_only.txt")
    git(repo, "commit", "-m", "PR-only change")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "main")
    cfg = config(repo, fake_model)
    cfg["repos"][0]["github"] = "owner/repo"

    def fake_gh(root, *args):
        if args[:2] == ("run", "list"):
            assert "headSha" in args[args.index("--json") + 1]
            return json.dumps([{"databaseId": 42, "headBranch": "feature", "headSha": sha}])
        if args[:2] == ("pr", "list"):
            return json.dumps([{"number": 7, "headRefName": "feature", "isCrossRepository": False,
                                "author": {"login": "human"}}])
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

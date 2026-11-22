import os
import signal
import subprocess
from unittest.mock import Mock

from autofix.agent import command


def test_timeout_kills_entire_process_group(monkeypatch, tmp_path):
    process = Mock(pid=1234)
    process.communicate.side_effect = [subprocess.TimeoutExpired("sleep", 1), ("partial", "")]
    popen = Mock(return_value=process)
    killpg = Mock()
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(os, "killpg", killpg)
    result = command(tmp_path, "sleep 30 & wait", timeout=1)
    assert popen.call_args.kwargs["start_new_session"] is True
    killpg.assert_called_once_with(1234, signal.SIGKILL)
    assert process.communicate.call_count == 2
    assert result == {"returncode": 124, "output": "partial\nCommand timed out"}

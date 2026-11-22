import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import zipfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from autofix.gitops import git
from test_runner import fake_model


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    root = tmp_path_factory.mktemp("mac-build")
    shutil.copytree(ROOT / "macos", root / "macos")
    shutil.copytree(ROOT / "autofix", root / "autofix")
    (root / "autofix" / "source-link").symlink_to("__init__.py")
    shutil.copy2(ROOT / "pyproject.toml", root / "pyproject.toml")
    subprocess.run(["bash", str(root / "macos" / "build_app.sh")], check=True, capture_output=True, text=True)
    return root / "dist"


def test_bundle_structure_syntax_and_zip_permissions(bundle):
    app = bundle / "Inkling Autofix.app"
    contents = app / "Contents"
    info = plistlib.loads((contents / "Info.plist").read_bytes())
    assert info["CFBundleExecutable"] == "inkling-autofix"
    assert info["CFBundleIdentifier"] == "com.inkling.autofix"
    assert info["LSMinimumSystemVersion"] == "11.0"
    assert info["CFBundlePackageType"] == "APPL"
    launcher = contents / "MacOS" / info["CFBundleExecutable"]
    assert os.access(launcher, os.X_OK)
    subprocess.run(["bash", "-n", str(launcher)], check=True)
    resources = contents / "Resources"
    assert (resources / "autofix-src" / "autofix" / "proposals.py").is_file()
    assert (resources / "autofix-src" / "pyproject.toml").is_file()
    assert len((resources / "source.sha256").read_text().strip()) == 64
    assert (bundle / "READ ME FIRST.txt").exists()
    assert not any(path.name in {".git", ".venv", "tests", "__pycache__"} for path in app.rglob("*"))
    with zipfile.ZipFile(bundle / "Inkling-Autofix-mac.zip") as archive:
        launcher_entry = archive.getinfo("Inkling Autofix.app/Contents/MacOS/inkling-autofix")
        assert stat.S_IMODE(launcher_entry.external_attr >> 16) & 0o111 == 0o111
        link = archive.getinfo("Inkling Autofix.app/Contents/Resources/autofix-src/autofix/source-link")
        assert stat.S_ISLNK(link.external_attr >> 16)
        assert archive.read(link) == b"__init__.py"
        assert "READ ME FIRST.txt" in archive.namelist()


def executable(path, body):
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o755)


@pytest.mark.parametrize("connection", ["Direct Tinker", "KeyShield hosted", "KeyShield local ZK proxy", "Cancel"])
def test_first_setup_and_source_hash_refresh_without_network(bundle, tmp_path, connection):
    bins = tmp_path / "bin"
    bins.mkdir()
    executable(bins / "osascript", '''import os, sys
from pathlib import Path
script = sys.stdin.read()
if "How should Inkling Autofix connect?" in script:
    assert sys.argv[2:] == ["Direct Tinker"]
    with Path(os.environ["PROMPT_COUNT"]).open("a") as stream: stream.write("prompted\\n")
    print("" if os.environ["FIRST_CONNECTION"] == "Cancel" else os.environ["FIRST_CONNECTION"])
elif "default answer (item 2 of argv)" in script:
    print("https://keyshield.example.invalid")
''')
    installer = bins / "managed-python"
    executable(installer, '''import os, sys
from pathlib import Path
if sys.argv[1:3] == ["-m", "pip"]:
    source = Path(sys.argv[-2])
    assert sys.argv[-1] == "pytest"
    assert source.parent == Path(os.environ["HOME"]) / "Library/Application Support/Inkling Autofix"
    assert (source / "autofix/proposals.py").exists()
    with Path(os.environ["INSTALL_COUNT"]).open("a") as stream: stream.write("installed\\n")
else:
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
''')
    bootstrap = bins / "bootstrap-python"
    executable(bootstrap, '''import os, shutil, sys
from pathlib import Path
assert sys.argv[1:4] == ["-m", "venv", "--clear"]
target = Path(sys.argv[4]) / "bin"
target.mkdir(parents=True, exist_ok=True)
shutil.copy2(os.environ["MANAGED_PYTHON"], target / "python")
''')
    launcher = bundle / "Inkling Autofix.app/Contents/MacOS/inkling-autofix"
    functions = launcher.read_text().split("\nfind_python || exit 1\n")[0]
    driver = tmp_path / "setup-test.sh"
    driver.write_text(functions + '''
CONTENTS="$TEST_CONTENTS"
SOURCE="$CONTENTS/Resources/autofix-src"
PYTHON="$BOOTSTRAP_PYTHON"
setup || exit 1
PYTHON="$BOOTSTRAP_PYTHON"
setup || exit 1
printf 'old source hash' > "$DATA/installed-source.sha256"
PYTHON="$BOOTSTRAP_PYTHON"
setup || exit 1
''')
    count = tmp_path / "install-count"
    env = {**os.environ, "HOME": str(tmp_path / "home"), "PATH": str(bins) + os.pathsep + os.environ["PATH"],
           "TEST_CONTENTS": str(launcher.parent.parent), "MANAGED_PYTHON": str(installer),
           "BOOTSTRAP_PYTHON": str(bootstrap), "INSTALL_COUNT": str(count),
           "FIRST_CONNECTION": connection, "PROMPT_COUNT": str(tmp_path / "prompt-count")}
    result = subprocess.run(["bash", str(driver)], env=env, capture_output=True, text=True, timeout=30)
    data = Path(env["HOME"]) / "Library/Application Support/Inkling Autofix"
    if connection == "Cancel":
        assert result.returncode == 1
        assert not (data / "model.yaml").exists()
        assert (tmp_path / "prompt-count").read_text().splitlines() == ["prompted"]
        return
    assert result.returncode == 0, result.stdout + result.stderr
    assert count.read_text().splitlines() == ["installed", "installed"]
    expected = {"provider": "tinker", "model": "thinkingmachines/Inkling:peft:262144", "tool_mode": "native"}
    if connection != "Direct Tinker":
        expected.update(provider="keyshield", keyshield_upstream="tinker", keyshield_base=(
            "http://127.0.0.1:8787" if connection == "KeyShield local ZK proxy" else "https://keyshield.example.invalid"))
    assert yaml.safe_load((data / "model.yaml").read_text()) == expected
    assert (tmp_path / "prompt-count").read_text().splitlines() == ["prompted"]
    assert not list(data.glob("setup-source.*"))


@pytest.fixture
def keyshield_endpoint():
    state = {"requests": []}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append({"path": self.path, "authorization": self.headers.get("Authorization"), "body": body})
            tool, arguments = (("write_file", {"path": "selected/code.txt", "content": "fixed"})
                               if len(state["requests"]) == 1 else ("finish", {"summary": "Propose a fixture fix"}))
            response = {"id": "fixture", "object": "chat.completion", "created": 0, "model": "fixture",
                        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                            "role": "assistant", "tool_calls": [{"id": "fixture-call", "type": "function",
                            "function": {"name": tool, "arguments": json.dumps(arguments)}}]}}]}
            data = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["base"] = f"http://127.0.0.1:{server.server_port}"
    def stop():
        if thread.is_alive():
            server.shutdown()
            server.server_close()
            thread.join()
    state["stop"] = stop
    yield state
    stop()


@pytest.mark.parametrize("scenario", ["apply", "cancel", "stale", "scope", "failure", "settings", "tests", "quoted-key",
                                      "keyshield", "keyshield-down", "connections", "switch"])
def test_launcher_folder_flow_with_native_tools_stubbed(bundle, tmp_path, fake_model, keyshield_endpoint, scenario):
    home = tmp_path / "home with spaces"
    data = home / "Library" / "Application Support" / "Inkling Autofix"
    (data / "venv" / "bin").mkdir(parents=True)
    source = bundle / "Inkling Autofix.app" / "Contents" / "Resources" / "autofix-src"
    shutil.copy2(source.parent / "source.sha256", data / "installed-source.sha256")
    model = {**fake_model["model"], "provider": "tinker", "api_key_env": "TINKER_API_KEY"}
    if scenario in {"keyshield", "keyshield-down"}:
        model.pop("base_url")
        model.update(provider="keyshield", api_key_env="KS_TOKEN", keyshield_upstream="tinker",
                     keyshield_base=keyshield_endpoint["base"])
    if scenario == "keyshield-down":
        keyshield_endpoint["stop"]()
    (data / "model.yaml").write_text(yaml.safe_dump(model))
    project = tmp_path / "project with spaces"
    (project / "selected").mkdir(parents=True)
    target = project / "selected" / "code.txt"
    target.write_text("broken")
    (project / "outside.txt").write_text("do not change")
    (project / ".env").write_text("fixture-configuration-not-for-model")
    subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
    git(project, "config", "user.name", "Test")
    git(project, "config", "user.email", "test@example.invalid")
    git(project, "add", "--all")
    git(project, "commit", "-m", "baseline")
    head = git(project, "rev-parse", "HEAD")
    state = tmp_path / "state"
    state.mkdir()
    bins = tmp_path / "stub tools"
    bins.mkdir()
    events = state / "events.jsonl"
    test_key = 'launcher-test-key' + (r' "quoted" \ tail; $(ignored)' if scenario == "quoted-key" else "")
    if scenario in {"keyshield", "keyshield-down", "switch"}:
        test_key = "local-fixture-token"
    common = '''import json, os, sys
from pathlib import Path
state = Path(os.environ["STUB_STATE"])
key = "launcher-" + "test-key"
if os.environ["STUB_SCENARIO"] == "quoted-key": key += r' "quoted" \\ tail; $(ignored)'
def credential(account):
    return key if account == "tinker" else ("local-" if account == "keyshield-local" else "hosted-") + "fixture-token"
def record(kind, **fields):
    with (state / "events.jsonl").open("a") as stream:
        stream.write(json.dumps({"kind": kind, **fields}) + "\\n")
'''
    executable(data / "venv" / "bin" / "python", common + '''
args = sys.argv[1:]
proposing = args[:3] == ["-m", "autofix", "propose"]
from pathlib import Path
import yaml
settings = yaml.safe_load(Path(os.environ["STUB_MODEL"]).read_text()) if Path(os.environ["STUB_MODEL"]).exists() else {}
keyshield = settings.get("provider") == "keyshield"
assert ("TINKER_API_KEY" in os.environ) == (proposing and not keyshield)
assert ("KS_TOKEN" in os.environ) == (proposing and keyshield)
record("python", args=args, tinker_present="TINKER_API_KEY" in os.environ, ks_present="KS_TOKEN" in os.environ)
if proposing:
    from openai import OpenAI
    from urllib.parse import urlsplit
    initialize = OpenAI.__init__
    def guarded_initialize(self, *args, **kwargs):
        record("model-client", url=kwargs["base_url"])
        assert urlsplit(kwargs["base_url"]).hostname == "127.0.0.1", "Tests must not contact a live upstream"
        return initialize(self, *args, **kwargs)
    OpenAI.__init__ = guarded_initialize
    from autofix.cli import main
    sys.argv = ["autofix", *args[2:]]
    main()
    sys.exit(0)
os.execv(sys.executable, [sys.executable, *args])
''')
    shutil.copy2(data / "venv" / "bin" / "python", data / "venv" / "bin" / "python3")
    executable(bins / "security", common + '''
import shlex
assert "TINKER_API_KEY" not in os.environ and "KS_TOKEN" not in os.environ
assert all(credential(account) not in " ".join(sys.argv) for account in ["tinker", "keyshield", "keyshield-local"])
args = shlex.split(sys.stdin.read()) if sys.argv[1:] == ["-i"] else sys.argv[1:]
operation = args[0]
account = args[args.index("-a") + 1]
record("security", operation=operation, account=account, args=sys.argv[1:])
assert "Inkling Autofix" in args and account in {"tinker", "keyshield", "keyshield-local"}
item = state / ("credential-" + account)
if operation == "find-generic-password":
    if not item.exists(): sys.exit(44)
    print(credential(account))
elif operation == "add-generic-password":
    assert sys.argv[1:] == ["-i"] and args[-1] == credential(account)
    item.touch()
elif operation == "delete-generic-password":
    item.unlink(missing_ok=True)
else: raise AssertionError(operation)
''')
    executable(bins / "open", common + '''
assert "TINKER_API_KEY" not in os.environ and "KS_TOKEN" not in os.environ
record("open", args=sys.argv[1:])
''')
    executable(bins / "osascript", common + '''
assert "TINKER_API_KEY" not in os.environ and "KS_TOKEN" not in os.environ
script = sys.stdin.read()
args = sys.argv[2:] if sys.argv[1:2] == ["-"] else sys.argv[1:]
scenario = os.environ["STUB_SCENARIO"]
target = Path(os.environ["STUB_TARGET"])
record("dialog", args=args)
def count(name):
    path = state / name
    value = int(path.read_text()) if path.exists() else 0
    path.write_text(str(value + 1))
    return value
if "display notification" in script:
    pass
elif "with hidden answer" in script:
    account = "tinker" if args[0] == "Enter your Tinker API key:" else "keyshield-local" if args[0] == "Enter the local token printed by keyshield-proxy" else "keyshield"
    if account == "keyshield": assert args[0] == "Enter your KeyShield proxy-only agent token (Developer > Create agent token)"
    print(credential(account))
elif "What would you like to do?" in script:
    n = count("main")
    expected_connection = "KeyShield local ZK proxy" if scenario in {"keyshield", "keyshield-down"} or (scenario == "switch" and n > 0) else "Direct Tinker"
    assert args == [expected_connection]
    print("Settings" if scenario in {"settings", "connections", "switch"} and n == 0 else "Quit" if scenario == "connections" else "Fix something in a folder" if n < 2 else "Quit")
elif "How should Inkling Autofix connect?" in script:
    assert scenario in {"connections", "switch"}, "Existing settings must not prompt for a connection"
    n = count("connection")
    print("KeyShield hosted" if scenario == "switch" or n < 2 else "KeyShield local ZK proxy" if n == 2 else "Direct Tinker")
elif "choose folder" in script:
    n = count("target")
    print(target.parent if scenario == "scope" and n == 0 else target.parent.parent)
elif "How would you like to fix it?" in script:
    print("Fix failing tests" if scenario == "tests" else "Describe the bug")
elif "Provide failing output." in script:
    print("Paste failing test output")
elif "default answer (item 2 of argv)" in script:
    if args[0].startswith("Enter a model ID"):
        print("tinker://run/sampler_weights/000080")
    elif args[0].startswith("Paste the failing test output"):
        print("AssertionError: broken value\\nExpected: fixed")
    elif args[0].startswith("Enter the KeyShield hosted base URL"):
        n = count("base")
        print(os.environ["STUB_PROXY"] if scenario == "switch" else "http://example.invalid" if n == 0 else "https://hosted.example.invalid")
    else:
        print('Fix the "empty" value; $(touch SHOULD_NOT_EXIST)')
elif "Review proposed changes" in script:
    assert target.read_text() == "broken", "User files changed before approval"
    assert "selected/code.txt" in args[0]
    assert "Validation: tests and commands were NOT run; review the diff yourself." in args[0]
    assert ".env" in args[0]
    if scenario == "stale": target.write_text("concurrent edit")
    print("Cancel" if scenario == "cancel" else "Apply changes")
elif "Choose a larger folder" in script:
    assert "outside.txt" in args[0]
    assert target.read_text() == "broken"
    print("Choose a larger folder")
elif "Open log" in script:
    print("Open log")
elif "Fix something else" in script:
    print("Quit")
elif "What would you like to change?" in script:
    choices = (["Change connection", "Reset API key", "Change connection", "Reset API key", "Change connection", "Reset API key", "Back"] if scenario == "connections" else ["Change connection", "Back"] if scenario == "switch" else ["Change model ID or checkpoint", "Change tool mode", "Reset API key", "Back"])
    print(choices[count("settings")])
elif "Choose the tool mode:" in script:
    print("json")
elif args and args[0].startswith("Could not save the connection."):
    assert "HTTPS" in args[0] and "loopback" in args[0]
    import yaml
    assert yaml.safe_load(Path(os.environ["STUB_MODEL"]).read_text())["provider"] == "tinker"
else:
    raise AssertionError(script)
''')
    script = [("write_file", {"path": "selected/code.txt", "content": "fixed"}),
              ("finish", {"summary": "Fix the selected code"})]
    if scenario == "scope":
        script = [("write_file", {"path": "outside.txt", "content": "outside edit"}),
                  ("finish", {"summary": "Needs larger scope"}), *script]
    if scenario == "settings":
        script = [json.dumps({"tool_calls": [{"name": name, "arguments": arguments}]}) for name, arguments in script]
    fake_model["script"] = [None] if scenario == "failure" else script
    env = {**os.environ, "HOME": str(home), "PATH": str(bins) + os.pathsep + os.environ["PATH"],
           "PYTHONPATH": str(source), "STUB_STATE": str(state), "STUB_SCENARIO": scenario,
           "STUB_TARGET": str(target), "TINKER_API_KEY": "do-not-use-inherited-key", "KS_TOKEN": "do-not-use-inherited-token",
           "STUB_MODEL": str(data / "model.yaml"), "STUB_PROXY": keyshield_endpoint["base"]}
    launcher = bundle / "Inkling Autofix.app" / "Contents" / "MacOS" / "inkling-autofix"
    originals = {p.relative_to(project): p.read_bytes() for p in project.rglob("*")
                 if p.is_file() and ".git" not in p.relative_to(project).parts}
    result = subprocess.run([str(launcher)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    recorded = [json.loads(line) for line in events.read_text().splitlines()]
    calls = [entry["args"] for entry in recorded if entry["kind"] == "python"]
    applies = [args for args in calls if args[:3] == ["-m", "autofix", "apply"]]
    assert len(applies) == (0 if scenario in {"cancel", "failure", "keyshield-down", "connections"} else 1)
    expected = "concurrent edit" if scenario == "stale" else "broken" if scenario in {"cancel", "failure", "keyshield-down", "connections"} else "fixed"
    assert target.read_text() == expected
    if scenario == "cancel":
        assert originals == {p.relative_to(project): p.read_bytes() for p in project.rglob("*")
                             if p.is_file() and ".git" not in p.relative_to(project).parts}
    assert (project / "outside.txt").read_text() == "do not change"
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()
    assert git(project, "rev-parse", "HEAD") == head
    assert git(project, "branch", "--show-current") == "main"
    if scenario in {"keyshield", "switch"}:
        assert len(keyshield_endpoint["requests"]) == 2
        assert all(request["path"] == "/vproxy/tinker/chat/completions" and request["authorization"] == "Bearer " + test_key
                   for request in keyshield_endpoint["requests"])
        assert fake_model["calls"] == []
    elif scenario not in {"keyshield-down", "connections"}:
        assert fake_model["authorization"] == "Bearer " + test_key
    tokens = {test_key, "hosted-fixture-token", "local-fixture-token", "launcher-test-key"}
    assert all(token not in str(entry) for entry in recorded for token in tokens)
    for path in data.rglob("*"):
        if path.is_file():
            assert all(token.encode() not in path.read_bytes() for token in tokens), str(path)
    if scenario == "connections":
        security = [entry for entry in recorded if entry["kind"] == "security"]
        assert [entry["account"] for entry in security if entry["operation"] == "delete-generic-password"] == ["keyshield", "keyshield-local", "tinker"]
        assert {entry["account"] for entry in security if entry["operation"] == "add-generic-password"} == {"tinker", "keyshield", "keyshield-local"}
        assert yaml.safe_load((data / "model.yaml").read_text())["provider"] == "tinker"
        assert not any(entry["kind"] == "model-client" for entry in recorded)
    elif scenario in {"failure", "stale", "keyshield-down"}:
        assert any(entry["kind"] == "open" and entry["args"][-1].endswith("launcher.log") for entry in recorded)
    else:
        assert any(entry["kind"] == "open" and entry["args"][-1].endswith("proposal.diff") for entry in recorded)
    if scenario == "scope":
        assert len([args for args in calls if args[:3] == ["-m", "autofix", "propose"]]) == 2
    if scenario == "settings":
        settings = yaml.safe_load((data / "model.yaml").read_text())
        assert settings["tool_mode"] == "json"
        assert settings["model"] == "tinker://run/sampler_weights/000080"
        assert any(entry.get("operation") == "delete-generic-password" for entry in recorded)
        assert "tools" not in fake_model["calls"][0]
    if scenario == "tests":
        assert not any(args[:2] == ["-m", "pytest"] for args in calls)
        proposing = next(args for args in calls if args[:3] == ["-m", "autofix", "propose"])
        assert "--test-output" in proposing and "--test-command" not in proposing
        pasted = Path(proposing[proposing.index("--test-output") + 1])
        assert "Expected: fixed" in pasted.read_text()
    if scenario in {"keyshield", "keyshield-down"}:
        assert {entry["account"] for entry in recorded if entry["kind"] == "security"} == {"keyshield-local"}
    if scenario == "keyshield-down":
        assert any(entry["kind"] == "dialog" and "KeyShield request failed. Nothing was sent to Tinker directly." in str(entry["args"])
                   for entry in recorded)
        assert keyshield_endpoint["requests"] == [] and fake_model["calls"] == []
        clients = [entry for entry in recorded if entry["kind"] == "model-client"]
        assert clients and all(entry["url"] == keyshield_endpoint["base"] + "/vproxy/tinker" for entry in clients)

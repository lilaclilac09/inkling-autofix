import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import zipfile
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


def test_first_setup_and_source_hash_refresh_without_network(bundle, tmp_path):
    bins = tmp_path / "bin"
    bins.mkdir()
    executable(bins / "osascript", "import sys\nsys.stdin.read()\n")
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
           "BOOTSTRAP_PYTHON": str(bootstrap), "INSTALL_COUNT": str(count)}
    subprocess.run(["bash", str(driver)], env=env, check=True, capture_output=True, text=True, timeout=30)
    assert count.read_text().splitlines() == ["installed", "installed"]
    data = Path(env["HOME"]) / "Library/Application Support/Inkling Autofix"
    assert yaml.safe_load((data / "model.yaml").read_text()) == {
        "provider": "tinker", "model": "thinkingmachines/Inkling:peft:262144", "tool_mode": "native"}
    assert not list(data.glob("setup-source.*"))


@pytest.mark.parametrize("scenario", ["apply", "cancel", "stale", "scope", "failure", "settings", "tests"])
def test_launcher_folder_flow_with_native_tools_stubbed(bundle, tmp_path, fake_model, scenario):
    home = tmp_path / "home with spaces"
    data = home / "Library" / "Application Support" / "Inkling Autofix"
    (data / "venv" / "bin").mkdir(parents=True)
    source = bundle / "Inkling Autofix.app" / "Contents" / "Resources" / "autofix-src"
    shutil.copy2(source.parent / "source.sha256", data / "installed-source.sha256")
    (data / "model.yaml").write_text(yaml.safe_dump({**fake_model["model"], "provider": "tinker",
                                                    "api_key_env": "TINKER_API_KEY"}))
    project = tmp_path / "project with spaces"
    (project / "selected").mkdir(parents=True)
    target = project / "selected" / "code.txt"
    target.write_text("broken")
    (project / "outside.txt").write_text("do not change")
    if scenario == "tests":
        (project / "test_code.py").write_text(
            "from pathlib import Path\n"
            "def test_fixed():\n"
            "    assert (Path(__file__).parent / 'selected/code.txt').read_text() == 'fixed'\n")
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
    common = '''import json, os, sys
from pathlib import Path
state = Path(os.environ["STUB_STATE"])
def record(kind, **fields):
    with (state / "events.jsonl").open("a") as stream:
        stream.write(json.dumps({"kind": kind, **fields}) + "\\n")
'''
    executable(data / "venv" / "bin" / "python", common + '''
args = sys.argv[1:]
proposing = args[:3] == ["-m", "autofix", "propose"]
assert ("TINKER_API_KEY" in os.environ) == proposing
record("python", args=args, key_present="TINKER_API_KEY" in os.environ)
os.execv(sys.executable, [sys.executable, *args])
''')
    shutil.copy2(data / "venv" / "bin" / "python", data / "venv" / "bin" / "python3")
    executable(bins / "security", common + '''
assert "TINKER_API_KEY" not in os.environ
operation = sys.argv[1]
record("security", operation=operation)
assert "Inkling Autofix" in sys.argv and "tinker" in sys.argv
if operation == "find-generic-password":
    if not (state / "credential").exists(): sys.exit(44)
    print("launcher-test-key")
elif operation == "add-generic-password":
    assert sys.argv[-1] == "launcher-test-key"
    (state / "credential").touch()
elif operation == "delete-generic-password":
    (state / "credential").unlink(missing_ok=True)
else: raise AssertionError(operation)
''')
    executable(bins / "open", common + '''
assert "TINKER_API_KEY" not in os.environ
record("open", args=sys.argv[1:])
''')
    executable(bins / "osascript", common + '''
assert "TINKER_API_KEY" not in os.environ
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
elif "Enter your Tinker API key:" in script:
    print("launcher-test-key")
elif "What would you like to do?" in script:
    n = count("main")
    print("Settings" if scenario == "settings" and n == 0 else "Fix something in a folder" if n < 2 else "Quit")
elif "choose folder" in script:
    n = count("target")
    print(target.parent if scenario == "scope" and n == 0 else target.parent.parent)
elif "How would you like to fix it?" in script:
    print("Fix failing tests" if scenario == "tests" else "Describe the bug")
elif "default answer (item 2 of argv)" in script:
    if args[0].startswith("Enter a model ID"):
        print("tinker://run/sampler_weights/000080")
    elif args[0].startswith("Enter the test command"):
        assert args[1] == "python3 -m pytest -q"
        print(args[1])
    else:
        print('Fix the "empty" value; $(touch SHOULD_NOT_EXIST)')
elif "Review proposed changes" in script:
    assert target.read_text() == "broken", "User files changed before approval"
    assert "selected/code.txt" in args[0]
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
    choices = ["Change model ID or checkpoint", "Change tool mode", "Reset API key", "Back"]
    print(choices[count("settings")])
elif "Choose the tool mode:" in script:
    print("json")
else:
    raise AssertionError(script)
''')
    script = [("run_command", {"command": 'test -z "${TINKER_API_KEY+x}" && echo key-absent'}),
              ("write_file", {"path": "selected/code.txt", "content": "fixed"}),
              ("finish", {"summary": "Fix the selected code"})]
    if scenario == "scope":
        script = [("write_file", {"path": "outside.txt", "content": "outside edit"}),
                  ("finish", {"summary": "Needs larger scope"}), *script]
    if scenario == "settings":
        script = [json.dumps({"tool_calls": [{"name": name, "arguments": arguments}]}) for name, arguments in script]
    fake_model["script"] = [None] if scenario == "failure" else script
    env = {**os.environ, "HOME": str(home), "PATH": str(bins) + os.pathsep + os.environ["PATH"],
           "PYTHONPATH": str(source), "STUB_STATE": str(state), "STUB_SCENARIO": scenario,
           "STUB_TARGET": str(target), "TINKER_API_KEY": "do-not-use-inherited-key"}
    launcher = bundle / "Inkling Autofix.app" / "Contents" / "MacOS" / "inkling-autofix"
    result = subprocess.run([str(launcher)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    recorded = [json.loads(line) for line in events.read_text().splitlines()]
    calls = [entry["args"] for entry in recorded if entry["kind"] == "python"]
    applies = [args for args in calls if args[:3] == ["-m", "autofix", "apply"]]
    assert len(applies) == (0 if scenario in {"cancel", "failure"} else 1)
    expected = "concurrent edit" if scenario == "stale" else "broken" if scenario in {"cancel", "failure"} else "fixed"
    assert target.read_text() == expected
    assert (project / "outside.txt").read_text() == "do not change"
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()
    assert git(project, "rev-parse", "HEAD") == head
    assert git(project, "branch", "--show-current") == "main"
    assert fake_model["authorization"] == "Bearer launcher-test-key"
    assert all("launcher-test-key" not in str(args) for args in calls)
    for path in data.rglob("*"):
        if path.is_file():
            assert b"launcher-test-key" not in path.read_bytes(), str(path)
    if scenario in {"failure", "stale"}:
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
        assert ["-m", "pytest", "-q"] in calls

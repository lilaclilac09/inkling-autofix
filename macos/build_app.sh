#!/bin/bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
DIST="$ROOT/dist"
mkdir -p "$DIST"

python3 - "$ROOT" "$DIST" <<'PY'
import hashlib
import plistlib
import shutil
import sys
from pathlib import Path

root, dist = map(Path, sys.argv[1:])
app = dist / "Inkling Autofix.app"
if app.exists():
    shutil.rmtree(app)
contents = app / "Contents"
macos = contents / "MacOS"
resources = contents / "Resources"
macos.mkdir(parents=True)
resources.mkdir()
source = resources / "autofix-src"
source.mkdir()
shutil.copytree(root / "autofix", source / "autofix", symlinks=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".venv", "tests"))
shutil.copy2(root / "pyproject.toml", source / "pyproject.toml")
launcher = macos / "inkling-autofix"
shutil.copy2(root / "macos" / "launcher.sh", launcher)
launcher.chmod(0o755)
with (contents / "Info.plist").open("wb") as stream:
    plistlib.dump({
        "CFBundleExecutable": "inkling-autofix",
        "CFBundleIdentifier": "com.inkling.autofix",
        "CFBundleName": "Inkling Autofix",
        "CFBundleDisplayName": "Inkling Autofix",
        "CFBundlePackageType": "APPL",
        "CFBundleVersion": "1",
        "CFBundleShortVersionString": "0.1.0",
        "LSMinimumSystemVersion": "11.0",
        "LSUIElement": True,
    }, stream)
hasher = hashlib.sha256()
for path in sorted(source.rglob("*")):
    if path.is_file():
        hasher.update(path.relative_to(source).as_posix().encode() + b"\0")
        data = path.read_bytes()
        hasher.update(str(len(data)).encode() + b"\0" + data)
(resources / "source.sha256").write_text(hasher.hexdigest() + "\n")
shutil.copy2(root / "macos" / "READ ME FIRST.txt", dist / "READ ME FIRST.txt")
PY

rm -f "$DIST/Inkling-Autofix-mac.zip"
(
    cd "$DIST"
    zip -r -y "Inkling-Autofix-mac.zip" "Inkling Autofix.app" "READ ME FIRST.txt"
)
printf '\nBuilt: %s\n' "$DIST/Inkling-Autofix-mac.zip"

#!/bin/bash
set +x
set -uo pipefail
umask 077

CONTENTS=$(cd "$(dirname "$0")/.." && pwd)
SOURCE="$CONTENTS/Resources/autofix-src"
DATA="$HOME/Library/Application Support/Inkling Autofix"
VENV="$DATA/venv"
MODEL="$DATA/model.yaml"
LOG="$DATA/logs/launcher.log"
mkdir -p "$DATA/logs" "$DATA/proposals"
unset api_key TINKER_API_KEY INKLING_API_KEY OPENAI_API_KEY
api_key=""
trap 'unset api_key' EXIT

message() {
    osascript - "$1" <<'APPLESCRIPT'
on run argv
    tell current application to activate
    display dialog (item 1 of argv) with title "Inkling Autofix" buttons {"OK"} default button "OK"
end run
APPLESCRIPT
}

notice() {
    osascript - "$1" >/dev/null 2>&1 <<'APPLESCRIPT'
on run argv
    display notification (item 1 of argv) with title "Inkling Autofix"
end run
APPLESCRIPT
}

failure() {
    local details answer
    details=$(tail -n 20 "$LOG")
    answer=$(osascript - "$1" "$details" <<'APPLESCRIPT'
on run argv
    tell current application to activate
    set response to display dialog ((item 1 of argv) & return & return & (item 2 of argv)) with title "Inkling Autofix — Error" buttons {"Close", "Open log"} default button "Open log"
    return button returned of response
end run
APPLESCRIPT
    ) || return 0
    if [ "$answer" = "Open log" ]; then open -e "$LOG"; fi
}

find_python() {
    local candidate fallback
    fallback=$(command -v python3 || true)
    for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 \
        /Library/Frameworks/Python.framework/Versions/*/bin/python3 "$fallback"; do
        if [ -x "$candidate" ] && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
            PYTHON="$candidate"
            return 0
        fi
    done
    local answer
    answer=$(osascript <<'APPLESCRIPT'
tell current application to activate
set response to display dialog "Python 3.10 or newer is required. Install it using the graphical installer at https://www.python.org/downloads/macos/, then reopen this app." with title "Inkling Autofix" buttons {"Quit", "Download Python"} default button "Download Python"
return button returned of response
APPLESCRIPT
    ) || return 1
    if [ "$answer" = "Download Python" ]; then open "https://www.python.org/downloads/macos/"; fi
    return 1
}

setup() {
    local bundled installed staging
    bundled=$(cat "$CONTENTS/Resources/source.sha256") || return 1
    installed=$(cat "$DATA/installed-source.sha256" 2>/dev/null || true)
    if [ "$bundled" != "$installed" ] || ! "$VENV/bin/python" -c 'import autofix, pytest' >/dev/null 2>&1; then
        notice "Setting up (first run only)…" || true
        printf '\nSetting up bundled source %s\n' "$bundled" >> "$LOG"
        staging=$(mktemp -d "$DATA/setup-source.XXXXXX") || { failure "Could not prepare setup files."; return 1; }
        if ! cp -R "$SOURCE/." "$staging/" >> "$LOG" 2>&1 \
            || ! "$PYTHON" -m venv --clear "$VENV" >> "$LOG" 2>&1 \
            || ! "$VENV/bin/python" -m pip install --disable-pip-version-check "$staging" pytest >> "$LOG" 2>&1; then
            rm -rf "$staging"
            failure "Setup failed. Check your Internet connection and Python installation."
            return 1
        fi
        rm -rf "$staging"
        printf '%s\n' "$bundled" > "$DATA/installed-source.sha256"
    fi
    PYTHON="$VENV/bin/python"
    if [ ! -f "$MODEL" ]; then
        "$PYTHON" - "$MODEL" >> "$LOG" 2>&1 <<'PY'
import sys
from pathlib import Path
import yaml
Path(sys.argv[1]).write_text(yaml.safe_dump({
    "provider": "tinker", "model": "thinkingmachines/Inkling:peft:262144", "tool_mode": "native"}))
PY
        if [ "$?" -ne 0 ]; then failure "Could not create model settings."; return 1; fi
    fi
}

get_api_key() {
    # KeyShield or another credential provider can replace this function later.
    local key quoted
    export -n key quoted
    if [ "${1:-}" = "reset" ]; then
        security delete-generic-password -s "Inkling Autofix" -a tinker >/dev/null 2>&1 || true
    elif key=$(security find-generic-password -s "Inkling Autofix" -a tinker -w 2>/dev/null) && [ -n "$key" ]; then
        printf '%s' "$key"
        return 0
    fi
    key=$(osascript <<'APPLESCRIPT'
tell current application to activate
text returned of (display dialog "Enter your Tinker API key:" with title "Inkling Autofix" default answer "" with hidden answer buttons {"Cancel", "Save key"} default button "Save key" cancel button "Cancel")
APPLESCRIPT
    ) || return 1
    if [ -z "$key" ]; then message "The API key cannot be empty." >/dev/null; return 1; fi
    case "$key" in *[![:print:]]*) message "The API key must not contain control characters." >/dev/null; return 1 ;; esac
    quoted=${key//\\/\\\\}
    quoted=${quoted//\"/\\\"}
    if ! printf 'add-generic-password -U -s "Inkling Autofix" -a tinker -w "%s"\n' "$quoted" | security -i >/dev/null 2>&1; then
        message "Could not save the API key in your login Keychain." >/dev/null
        return 1
    fi
    printf '%s' "$key"
}

ask_text() {
    osascript - "$1" "$2" <<'APPLESCRIPT'
on run argv
    tell current application to activate
    text returned of (display dialog (item 1 of argv) with title "Inkling Autofix" default answer (item 2 of argv) buttons {"Cancel", "Continue"} default button "Continue" cancel button "Cancel")
end run
APPLESCRIPT
}

model_setting() {
    "$PYTHON" - "$MODEL" "$@" <<'PY'
import sys
from pathlib import Path
import yaml
path, field = Path(sys.argv[1]), sys.argv[2]
settings = yaml.safe_load(path.read_text())
if len(sys.argv) == 4:
    settings[field] = sys.argv[3]
    path.write_text(yaml.safe_dump(settings))
else:
    print(settings.get(field, ""))
PY
}

settings() {
    local choice value current
    while true; do
        choice=$(osascript <<'APPLESCRIPT'
tell current application to activate
set choice to choose from list {"Change model ID or checkpoint", "Change tool mode", "Reset API key", "Back"} with title "Inkling Autofix — Settings" with prompt "What would you like to change?"
if choice is false then return "Back"
return item 1 of choice
APPLESCRIPT
        ) || return 0
        case "$choice" in
            "Change model ID or checkpoint")
                current=$(model_setting model 2>> "$LOG") || { failure "Could not read model settings."; continue; }
                value=$(ask_text "Enter a model ID or Tinker sampler checkpoint:" "$current") || continue
                if [ -z "$value" ]; then message "The model ID cannot be empty." >/dev/null; continue; fi
                model_setting model "$value" >> "$LOG" 2>&1 || failure "Could not save model settings."
                ;;
            "Change tool mode")
                value=$(osascript <<'APPLESCRIPT'
tell current application to activate
set choice to choose from list {"native", "json"} with title "Inkling Autofix" with prompt "Choose the tool mode:" default items {"native"}
if choice is false then return ""
return item 1 of choice
APPLESCRIPT
                ) || continue
                case "$value" in native|json) model_setting tool_mode "$value" >> "$LOG" 2>&1 || failure "Could not save tool mode." ;; esac
                ;;
            "Reset API key") api_key=$(get_api_key reset) || api_key="" ;;
            *) return 0 ;;
        esac
    done
}

finish_flow() {
    local answer
    answer=$(osascript - "$1" <<'APPLESCRIPT'
on run argv
    tell current application to activate
    set response to display dialog (item 1 of argv) with title "Inkling Autofix" buttons {"Quit", "Fix something else"} default button "Fix something else"
    return button returned of response
end run
APPLESCRIPT
    ) || return 1
    [ "$answer" = "Fix something else" ]
}

proposal_flow() {
    local kind="$1" target request task test_output pasted input_kind input_file stamp proposal run_log status answer review_text
    while true; do
        if [ "$kind" = "file" ]; then
            target=$(osascript <<'APPLESCRIPT'
tell current application to activate
POSIX path of (choose file with prompt "Choose the single file to fix:")
APPLESCRIPT
            ) || return 0
        else
            target=$(osascript <<'APPLESCRIPT'
tell current application to activate
POSIX path of (choose folder with prompt "Choose the project folder to fix:")
APPLESCRIPT
            ) || return 0
        fi
        request=$(osascript <<'APPLESCRIPT'
tell current application to activate
set choice to choose from list {"Describe the bug", "Fix failing tests"} with title "Inkling Autofix" with prompt "How would you like to fix it?"
if choice is false then return ""
return item 1 of choice
APPLESCRIPT
        ) || return 0
        stamp="$(date +%Y%m%d-%H%M%S)-$$-$RANDOM"
        proposal="$DATA/proposals/$stamp"
        run_log="$DATA/logs/proposal-$stamp.log"
        case "$request" in
            "Describe the bug")
                task=$(ask_text "Describe the bug in one sentence:" "") || return 0
                if [ -z "$task" ]; then message "Please enter a bug description." >/dev/null; continue; fi
                ;;
            "Fix failing tests")
                input_kind=$(osascript <<'APPLESCRIPT'
tell current application to activate
set choice to choose from list {"Paste failing test output", "Choose an output file"} with title "Inkling Autofix" with prompt "Provide failing output. Tests and commands will NOT be run. Redact sensitive data first."
if choice is false then return ""
return item 1 of choice
APPLESCRIPT
                ) || return 0
                test_output="$proposal/test-output.txt"
                case "$input_kind" in
                    "Paste failing test output")
                        pasted=$(ask_text "Paste the failing test output (multiple lines are accepted). Redact sensitive data first:" $'\n\n\n') || return 0
                        if [ -z "$pasted" ]; then message "Please provide failing test output." >/dev/null; continue; fi
                        mkdir -p "$proposal"
                        printf '%s\n' "$pasted" > "$test_output"
                        ;;
                    "Choose an output file")
                        input_file=$(osascript <<'APPLESCRIPT'
tell current application to activate
POSIX path of (choose file with prompt "Choose a text file containing failing test output (redact sensitive data first):")
APPLESCRIPT
                        ) || return 0
                        mkdir -p "$proposal"
                        if ! "$PYTHON" - "$input_file" "$test_output" >> "$LOG" 2>&1 <<'PY'
import os
import sys
from pathlib import Path
from autofix.proposals import excluded, selected_path, text_file
source = selected_path(sys.argv[1])
if any(excluded(part) for part in source.parts):
    raise ValueError("Choose a non-secret text output file")
descriptor = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    data = text_file(descriptor, source.name)["data"]
finally:
    os.close(descriptor)
Path(sys.argv[2]).write_bytes(data)
PY
                        then failure "Could not read the test output file."; return 0; fi
                        ;;
                    *) return 0 ;;
                esac
                ;;
            *) return 0 ;;
        esac
        if [ -z "$api_key" ]; then api_key=$(get_api_key) || return 0; fi
        notice "Working… this can take a few minutes" || true
        if [ "$request" = "Describe the bug" ]; then
            TINKER_API_KEY="$api_key" "$PYTHON" -m autofix propose --path "$target" --task "$task" --out "$proposal" --model-config "$MODEL" > "$run_log" 2>&1
        else
            TINKER_API_KEY="$api_key" "$PYTHON" -m autofix propose --path "$target" --fix-tests --test-output "$test_output" --out "$proposal" --model-config "$MODEL" > "$run_log" 2>&1
        fi
        status=$?
        cat "$run_log" >> "$LOG"
        if [ "$status" -eq 3 ]; then
            answer=$(osascript - "$(cat "$run_log")" <<'APPLESCRIPT'
on run argv
    tell current application to activate
    set response to display dialog ((item 1 of argv) & return & return & "The proposal changed files outside your selection.") with title "Inkling Autofix — Scope" buttons {"Cancel", "Choose a larger folder"} default button "Choose a larger folder" cancel button "Cancel"
    return button returned of response
end run
APPLESCRIPT
            ) || return 0
            if [ "$answer" = "Choose a larger folder" ]; then kind="folder"; continue; fi
            return 0
        elif [ "$status" -ne 0 ]; then
            failure "Could not prepare the proposal."
            finish_flow "No changes were applied."; return $?
        fi
        review_text=$("$PYTHON" - "$proposal" 2>> "$LOG" <<'PY'
import json
import sys
from pathlib import Path
proposal = Path(sys.argv[1])
manifest = json.loads((proposal / "manifest.json").read_text())
print("The diff is open in TextEdit. Review it before applying.\n")
print((proposal / "summary.txt").read_text())
print("Changed files:")
for entry in manifest["files"]:
    print(f"{entry['action']}: {entry['path']}")
if not manifest["files"]:
    print("None — no changes are needed.")
print("\nValidation: tests and commands were NOT run; review the diff yourself.")
print("\nSkipped paths (not shown to the model):")
for name in manifest.get("skipped_paths", []):
    print(name)
if not manifest.get("skipped_paths"):
    print("None")
PY
        ) || { failure "Could not read the proposal."; return 0; }
        if ! open -e "$proposal/proposal.diff" >> "$LOG" 2>&1; then
            failure "Could not open the diff in TextEdit. Nothing was applied."
            return 0
        fi
        answer=$(osascript - "$review_text" <<'APPLESCRIPT'
on run argv
    tell current application to activate
    set response to display dialog (item 1 of argv) with title "Inkling Autofix — Review proposed changes" buttons {"Cancel", "Apply changes"} default button "Cancel" cancel button "Cancel"
    return button returned of response
end run
APPLESCRIPT
        ) || answer="Cancel"
        if [ "$answer" != "Apply changes" ]; then
            finish_flow "Proposal kept for inspection. No changes were applied."; return $?
        fi
        if "$PYTHON" -m autofix apply --proposal "$proposal" >> "$LOG" 2>&1; then
            finish_flow "Changes applied successfully. Nothing was committed, pushed, or published."; return $?
        else
            failure "Could not apply the proposal. Review the log for details."
            finish_flow "Apply failed. Inspect your project before trying again."; return $?
        fi
    done
}

find_python || exit 1
BASE_PYTHON_DIR=$(dirname "$PYTHON")
setup || exit 1
export PATH="$VENV/bin:$BASE_PYTHON_DIR:$PATH"
api_key=$(get_api_key) || exit 0
while true; do
    choice=$(osascript <<'APPLESCRIPT'
tell current application to activate
set choice to choose from list {"Fix something in a folder", "Fix something in a single file", "Settings", "Quit"} with title "Inkling Autofix" with prompt "What would you like to do?"
if choice is false then return "Quit"
return item 1 of choice
APPLESCRIPT
    ) || break
    case "$choice" in
        "Fix something in a folder") proposal_flow folder || break ;;
        "Fix something in a single file") proposal_flow file || break ;;
        "Settings") settings ;;
        *) break ;;
    esac
done

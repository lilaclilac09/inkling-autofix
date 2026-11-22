import os

from .agent import command


def command_context(root, template, params):
    event_command = params.get("event_command") or os.environ.get("AUTOFIX_EVENT_COMMAND")
    if not event_command:
        raise ValueError("command trigger requires event_command or AUTOFIX_EVENT_COMMAND")
    for key in template.get("required_env", []):
        if key == "AUTOFIX_EVENT_COMMAND" and event_command:
            continue
        if not os.environ.get(key):
            raise ValueError(f"Missing required environment variable: {key}")
    result = command(root, event_command)
    if result["returncode"]:
        raise RuntimeError(f"Event command failed: {result['output']}")
    return [(None, result["output"])] if result["output"].strip() else []

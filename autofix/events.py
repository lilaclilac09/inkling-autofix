import json
import logging
import os

from .agent import command


def command_context(root, template, params, log_dir=None):
    event_command = params.get("event_command") or os.environ.get("AUTOFIX_EVENT_COMMAND")
    if not event_command:
        raise ValueError("command trigger requires event_command or AUTOFIX_EVENT_COMMAND")
    for key in template.get("required_env", []):
        if key == "AUTOFIX_EVENT_COMMAND" and event_command:
            continue
        if not os.environ.get(key):
            raise ValueError(f"Missing required environment variable: {key}")
    result = command(root, event_command, separate_output=True)
    if result["stderr"]:
        if log_dir is not None:
            with (log_dir / "event_diagnostics.jsonl").open("a") as stream:
                stream.write(json.dumps({"template": template["name"], "stderr": result["stderr"],
                                         "returncode": result["returncode"]}) + "\n")
        else:
            logging.getLogger(__name__).warning("Event adapter stderr: %s", result["stderr"])
    if result["returncode"]:
        raise RuntimeError(f"Event command failed: {result['output']}")
    return [(None, result["stdout"])] if result["stdout"].strip() else []

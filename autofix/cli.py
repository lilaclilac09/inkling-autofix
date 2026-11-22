import argparse
import json
import sys
from pathlib import Path

from .runner import DATA, daemon, load_config, run, templates
from .swarm import swarm


def main():
    parser = argparse.ArgumentParser(description="Inkling repository repair runner")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("init")
    commands.add_parser("templates")
    execute = commands.add_parser("run")
    execute.add_argument("template", choices=sorted(templates()))
    execute.add_argument("--repo", required=True)
    execute.add_argument("--task")
    execute.add_argument("--dry-run", action="store_true")
    commands.add_parser("daemon")
    parallel = commands.add_parser("swarm")
    parallel.add_argument("templates", nargs="*")
    parallel.add_argument("--automation", nargs="+", default=[])
    parallel.add_argument("--repo", required=True)
    parallel.add_argument("--agents", type=int, default=3)
    args = parser.parse_args()
    try:
        if args.action == "init":
            with args.config.open("x") as stream:
                stream.write((DATA / "config.example.yaml").read_text())
            print(f"Created {args.config}")
        elif args.action == "templates":
            catalog = templates()
            for category in sorted({t.get("category", "Core") for t in catalog.values()}):
                print(f"\n{category}")
                for name, template in sorted(catalog.items()):
                    if template.get("category", "Core") == category:
                        print(f"  {name:30} {template['description']}")
        else:
            config = load_config(args.config.resolve())
            logs = args.config.resolve().parent / "logs"
            if args.action == "daemon":
                daemon(config, logs)
            elif args.action == "swarm":
                if args.templates and args.automation:
                    raise ValueError("Use template names or --automation, not both")
                jobs = [(name, {}) for name in args.templates]
                if args.automation:
                    for name in args.automation:
                        item = next(a for a in config.get("automations", []) if a["name"] == name)
                        if item["repo"] != args.repo:
                            raise ValueError("Swarm automations must target --repo")
                        reserved = {"name", "template", "repo", "schedule_minutes", "enabled", "params"}
                        params = {**item.get("params", {}),
                                  **{k: v for k, v in item.items() if k not in reserved}}
                        jobs.append((item["template"], params))
                elif len(jobs) == 1:
                    jobs *= args.agents
                print(json.dumps(swarm(config, args.repo, jobs, args.agents, logs), indent=2))
            else:
                params = {"task": args.task} if args.task is not None else {}
                print(json.dumps(run(config, args.template, args.repo, params,
                                     args.dry_run, logs), ensure_ascii=False, indent=2))
    except KeyboardInterrupt:
        return
    except Exception as exc:
        print(f"autofix: {exc}", file=sys.stderr)
        sys.exit(1)

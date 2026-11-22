# Inkling Autofix

A small, self-hosted, "Devin-like" Python repair runner. Connect any model that supports **OpenAI Chat Completions tool calling** (including your Inkling model) to read code, run commands, edit files, check changes, and commit in local clones. Run it on a schedule and configure GitHub to open PRs automatically. Requires Python 3.10+; this is not a cloud service and does not require an actual OpenAI API key.

## Five-minute MacBook quick start

Install Python 3.10+ and Git first. Prepare a separate, clean repository clone, a Git commit identity, and the project's own test environment. Optionally install the GitHub CLI: `brew install gh && gh auth login`.

```sh
cd inkling-autofix
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
export INKLING_API_KEY='your-api-key'
autofix init
# Edit config.yaml: set base_url (usually ending in /v1), model name, and absolute repo path
# Set base_branch; uncomment github: owner/repo if you want PRs
# Set test_command to a working test command for the target project
autofix templates
autofix run fix_failing_tests --repo myrepo --dry-run
# Review the branch and commit, then set enabled: true for the automations you want
autofix daemon
```

`--dry-run` **still edits files, runs commands, and creates branches and local commits**; it only skips push/PR creation and issue comments. Commands run in the target repository; interpreters, test tools, and dependencies must already be available. For non-Python projects, change `test_command` to something like `npm test`.

Use `autofix --config /absolute/path/config.yaml ...` to select a configuration file; relative repo paths are resolved against that file's directory. `autofix init` does not overwrite existing files. API keys are read only from the configured environment variable; do not put them in the config. The model must support `tools`/`tool_calls`; unsupported models fail with a clear error instead of falling back to parsing chat text.

## Using the Tinker API

Tinker has an [OpenAI-compatible inference API](https://tinker-docs.thinkingmachines.ai/inference/openai/overview/).
Replace the entire `model` section with:

```yaml
model:
  provider: tinker
  api_key_env: TINKER_API_KEY
  model: thinkingmachines/Inkling:peft:262144
  tool_mode: native
  max_steps: 40
```

Export `TINKER_API_KEY` before running. The preset base URL is
`https://tinker.thinkingmachines.dev/services/tinker-prod/oai/api/v1`.
The OpenAI SDK sends `Authorization: Bearer <TINKER_API_KEY>`.
Use the exact Tinker model ID you have access to, or your sampling checkpoint path,
such as `tinker://YOUR-RUN/sampler_weights/000080` (not a training checkpoint).
To select a compatible context length, the documented form is
`thinkingmachines/Inkling:peft:262144;tinker://YOUR-RUN/sampler_weights/000080`.
`inkling-2-mini` is not a verified Tinker ID. Production inference requires separate access:
override `base_url` with `https://inference.thinkingmachines.dev/prod/oai/api/v1`
and use an inference model ID such as `thinkingmachines/Inkling:peft:262144:inference`
(prepend this plus `;` to a checkpoint path for a production checkpoint).

The [Chat Completions documentation](https://tinker-docs.thinkingmachines.ai/inference/openai/chat-completions/)
supports native tool calls on chat models, not all Base models. We leave `tool_choice`
and `parallel_tool_calls` at their recommended defaults. If your checkpoint returns
plain text instead, set `tool_mode: json` for **any** provider. Autofix then supplies
tool schemas in the prompt, requests a plain-text JSON object containing
`tool_calls: [{name, arguments}]`, and accepts either bare JSON or one fenced JSON block.
It does not send `tools` or `response_format` (Tinker does not support structured output).
Malformed responses fail closed; there is no automatic fallback or text execution.
Reasoning fields returned by the server are retained in conversation history.
These settings follow official documentation; real Tinker authentication, model access,
and checkpoint behavior have not been tested here without credentials.

## Templates

Template sources are in `templates/` (a link to the packaged directory `autofix/templates/`). They use Python `str.format` placeholders, not Jinja; write literal braces as `{{` / `}}`.

| Template | Behavior | Success checks / parameters |
| --- | --- | --- |
| `fix_failing_tests` | Run tests and fix failures | `test_command`, default `python -m pytest -q` |
| `fix_github_issues` | Use `gh issue list` to fetch open issues labeled bug; create a separate branch/PR per issue and comment with the PR link on success | `test_command`; requires `github` and `gh` |
| `fix_ci_failure` | Read the latest failed run on the configured base branch with `gh run view --log-failed` and fix it | `test_command`; requires `github` and `gh` |
| `lint_and_typecheck_fix` | Fix lint and type errors, then test | `lint_command`, default `ruff check . && mypy .`, plus `test_command` |
| `dependency_update` | Update outdated dependencies and lockfiles in small steps, avoiding major upgrades | `test_command` |
| `custom` | Run a free-form task | `--task`; no external check by default, so setting `check_command` is recommended |

```sh
autofix run custom --repo myrepo --task 'Fix the timezone bug in date parsing and add regression tests' --dry-run
```

Each automation in the config can specify extra template parameters directly or use `params: {test_command: "...", check_command: "..."}`. Manual runs use template defaults (the CLI only overrides task). If your tests do not use pytest, change the default command in the relevant template or run the daemon with automation-specific configuration. New YAML templates require name, description, trigger.type, and prompt, with an optional check_command. `config.example.yaml` is the root-level example; its packaged copy is at `autofix/config.example.yaml`.

The daemon runs enabled entries immediately on startup, then schedules them serially at `schedule_minutes` intervals; restarting resets the timers. `logs/runs.jsonl` stores each agent run's status, branch, check results, summary, and token usage; `logs/daemon.jsonl` stores scheduling errors. API keys and full model requests are not logged. Logs may contain code, command output, or issue contents; protect and rotate them yourself. Runs with no issues or failed CI runs are skipped normally. Failures leave the working state intact without automatically cleaning up or discarding changes. Subsequent runs reject a dirty working tree, requiring manual intervention.

## Running in the Mac background (optional launchd)

Copy `examples/com.aileen.autofix.plist` to `~/Library/LaunchAgents/`, replace all `/Users/aileen` paths with your own absolute paths, and fill in the key (set file permissions to `600` and do not commit it), or use a secure credential injection method appropriate for your setup.

```sh
chmod 600 ~/Library/LaunchAgents/com.aileen.autofix.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.aileen.autofix.plist
# Stop: launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.aileen.autofix.plist
```

launchd does not inherit terminal exports or virtual environments; the example specifies the venv executable, working directory, and PATH. Before running, make sure the log directory exists and Git/gh authentication works. Execution cannot continue while the machine is asleep. This example has not been verified on an actual macOS machine.

## Safety boundaries and limitations (read first)

- Use only with repositories you trust. A separate low-privilege user, container/VM, restricted network, and dedicated clone are recommended. Do not expose SSH private keys or production credentials.
- Each run creates `autofix/<template>-<timestamp>` from the local `base_branch`. There is no automatic fetch/pull; update the base yourself. A clean working tree is required. The runner's Git workflow never pushes the base branch. It commits only after successful completion and passing checks, and pushes/opens a PR only when GitHub is configured and gh is installed. Keep human review and branch protection enabled.
- File tools are restricted to the repository root and reject `.git`, path traversal, and symlinks pointing outside the repository. **run_command is an arbitrary shell with the repository as cwd, not a sandbox**: the model, tests, and dependency installation scripts can still access the host, network, and credentials, and may bypass the Git restrictions in the prompt. Do not treat path checks or dry-run as security isolation.
- Models make mistakes; issues, CI logs, and repository contents may contain prompt injection. Passing checks does not guarantee correctness; adjust the default test command for each project. Keep sensitive files away from the model; newly generated files may be included in commits, so review before publishing.
- Tool output is truncated to roughly 16K characters. Commands time out after 120 seconds by default, with a maximum of 600 seconds. max_steps limits chat rounds, not tokens or cost. There is no OS sandbox, budget control, persistent task queue, concurrency lock, or automatic conflict recovery. Do not run concurrently against the same repository. Issues that remain open will be attempted again on the next run (there is no deduplication state).
- Execution stops at `finish` or max_steps. Runs that do not finish or fail checks are not committed/pushed. Failures preserve the current branch and files for investigation. No diff means no empty commit/PR.

## Development and testing

```sh
pip install -e '.[test]'
pytest -q
autofix templates
```

Tests use a stdlib HTTP fake model server to repair genuinely failing tests in a temporary Git repository and verify branches, commits, checks, and error boundaries. No real API key is needed. Verify the actual Inkling endpoint, GitHub push/PR workflow, and launchd in your own environment.

In short: A minimal self-hosted tool-calling repair runner. Use an isolated clone, configure an OpenAI-compatible endpoint, and review generated branches and PRs. Shell access is **not sandboxed**.

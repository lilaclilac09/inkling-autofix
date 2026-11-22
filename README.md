# Inkling Autofix

A small, self-hosted Python automation and repair runner. Connect any model that supports **OpenAI Chat Completions tool calling** (including your Inkling model) to read code, run commands, edit files, check changes, and commit in local clones. Run it on a schedule and configure GitHub to open PRs automatically. Requires Python 3.10+; this is not a cloud service and does not require an actual OpenAI API key.

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

Use `autofix --config /absolute/path/config.yaml ...` to select a configuration file; relative repo paths are resolved against that file's directory. `autofix init` does not overwrite existing files. API keys are read only from the configured environment variable; do not put them in the config. Native mode requires `tools`/`tool_calls`; unsupported models fail with a clear error. Explicitly select `tool_mode: json` for the plain-text protocol described below.

## Mac app (no Terminal)

Download and unzip `Inkling-Autofix-mac.zip`, then move **Inkling Autofix.app** to
Applications or another writable folder. This app is unsigned: on the first launch,
right-click it, choose **Open**, and confirm. If needed, use **System Settings > Privacy
& Security > Open Anyway** after trying to open it. Do not disable Gatekeeper globally.

Install **Python 3.10+** using the graphical installer at
[python.org](https://www.python.org/downloads/macos/) first. Git is not needed for proposals or apply. No Terminal commands are required
for the app's normal workflow. First launch needs Internet access to create a private
virtual environment and install the bundled package plus pytest. Future launches reuse it;
a changed bundled source hash triggers setup again.

1. Double-click the app and enter your Tinker API key in the hidden-answer dialog. It is
   stored in your login Keychain under service **Inkling Autofix**, account **tinker**.
2. Choose **Fix something in a folder** or **Fix something in a single file**, select the
   target, and choose **Describe the bug** or **Fix failing tests**. For failing tests,
   paste the failing output (multiple lines) or choose a text output file; redact sensitive data first.
3. Wait for the proposal. The diff opens in TextEdit; the approval dialog shows the summary
   and changed files. Inspect the diff, then choose **Apply changes** or **Cancel**.
4. Only explicit approval invokes apply. Stale originals are refused. Out-of-scope proposals
   offer **Choose a larger folder** without applying anything. Errors offer to open the log.

**Settings** changes the model ID/checkpoint, selects native or JSON tools, and resets the
API key. Default: `thinkingmachines/Inkling:peft:262144`, Tinker, native tools. The app does
not commit, push, or publish. **Validation limits:** the proposal agent has file tools only;
it cannot execute commands or run tests. The review dialog states:
"Validation: tests and commands were NOT run; review the diff yourself."
It also lists skipped paths. Review the diff yourself and validate changes separately before trusting them.

Local data lives in `~/Library/Application Support/Inkling Autofix/`: `model.yaml`, `venv/`,
`proposals/`, and `logs/launcher.log`. The key is never written there or logged; only the
autofix proposal process receives it in its environment. To remove the credential without
launching the app, open **Keychain Access**, search **Inkling Autofix**, and delete the
generic-password item with account **tinker**. Deleting the app does not remove that item.
You can separately delete the Application Support folder to remove local data.

For developers, `bash macos/build_app.sh` builds the unsigned bundle on Linux or macOS:
`dist/Inkling Autofix.app` and `dist/Inkling-Autofix-mac.zip`, including `READ ME FIRST.txt`.
The archive preserves executable bits and symlinks. No Python runtime is bundled.

## Propose, review, then apply locally

These commands do not require `config.yaml`. They never commit to your project, create a
branch there, push, or publish. Select a folder or one file, inspect the proposal, and run
`apply` only after explicitly approving it:

```sh
export TINKER_API_KEY='your-api-key'
autofix propose --path /Users/me/project --task "Fix the empty-input crash" --out /Users/me/proposals/fix-1
# Or propose a fix from saved failing output (nothing is executed):
autofix propose --path /Users/me/project --fix-tests --test-output /Users/me/failing-output.txt --out /Users/me/proposals/fix-tests
# A single-file selection allows edits only to that file:
autofix propose --path /Users/me/project/src/parser.py --task "Handle empty input" --out /Users/me/proposals/parser

# Inspect proposal.diff and summary.txt. Only after approval:
autofix apply --proposal /Users/me/proposals/fix-1
```

Optional `--model-config /Users/me/model.yaml` accepts the model settings directly, without
a `model:` wrapper. For example:

```yaml
provider: tinker
model: thinkingmachines/Inkling:peft:262144
api_key_env: TINKER_API_KEY
tool_mode: native
max_steps: 40
```

Without that file, these Tinker settings are the defaults. You can override `base_url`,
choose another supported provider, or select `tool_mode: json`. Export the named key;
never store it in the YAML file. `--fix-tests` requires `--test-output FILE`.
Legacy `--test-command` is ignored with a warning; it never executes anything.

The workspace root is the enclosing Git toplevel, or the selected folder/file's parent
when there is no Git repository (discovered without invoking Git). **Only the selected
scope is copied**, never neighboring repository files. There is no Git initialization,
baseline commit, command execution, or test/check execution in the proposal path.
Python hashes and `difflib` produce the changes and diff. File tools enforce the scope
before reading or writing; an attempted out-of-scope write invalidates the proposal.

The copy never follows symlinks and skips special files, binary/undecodable files,
files over 1 MB, `.git`, `.env*`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa*`,
`id_ed25519*`, `.ssh/`, `.aws/`, `.netrc`, `.npmrc`, `.pypirc`, names containing
`credentials` or `secret`, virtual environments, dependencies and caches. These paths
are unavailable to file tools. `manifest.json` records skipped names, never contents.
Name/content filters are not a guarantee that ordinary source files contain no secrets;
select narrowly and redact sensitive data, including supplied failing output.

**Validation limits:** every proposal summary and successful CLI output states:
"Validation: tests and commands were NOT run; review the diff yourself."
No successful test result is inferred from the model's proposed changes.

Use a new or empty output directory for each proposal. It contains `proposal.diff`,
`summary.txt`, `manifest.json`, and new text contents under `contents/`. An empty
proposal is valid. Out-of-scope edits exit **3**, list the offending files, and produce no
applicable manifest. Other errors exit 1. Apply validates every
path, scope, original SHA-256, and payload before writing anything; symlinked targets
or parents, special files, stale originals, or newly
conflicting files cause refusal with their names. Individual file replacements are atomic,
but applying several files is not a filesystem transaction. Only apply trusted proposals:
the manifest records its destination root and must not be edited to redirect it.

For the separate run/daemon/swarm automation paths, all command subprocesses strip the configured model-key variable, `TINKER_API_KEY`,
`INKLING_API_KEY`, `OPENAI_API_KEY`, and names matching `AUTOFIX_*KEY*`. This also covers
existing run, daemon, swarm, checks, adapters, and notifier paths; the model client retains
its key. **This is not an OS sandbox**: shell commands run with your user privileges,
and environment filtering does not prevent reading secrets stored in files or other
host resources. Use trusted projects and an isolated account/container for untrusted code.
The file-only propose/apply workflow does not expose that shell tool or execute these commands.

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

### Full automation catalog and event adapters

All 23 catalog slugs are included alongside the original six templates. `autofix templates`
groups them into Monitoring & Triage, CI/CD & Release, Security, Project Management, and Core.
Every catalog YAML has a category, trigger, integration notes, and required environment list.
There are no hidden Slack/Pylon/Datadog/Sentry/Linear/MCP clients: those templates use a
trusted command adapter that fetches events or data with your own credentials:

```yaml
automations:
  - name: alerts
    template: datadog-alert
    repo: myrepo
    enabled: true
    schedule_minutes: 10
    event_command: /absolute/path/fetch-alerts.sh
    notify_command: /absolute/path/post-report.sh
```

Alternatively export `AUTOFIX_EVENT_COMMAND`. Nonempty stdout becomes untrusted event context;
empty stdout skips execution, and nonzero exit status fails the run. Output is bounded by
the normal command limit. Adapter scripts own authentication, cursors, filtering, and routing
(including thread IDs). `notify_command` receives the final summary on stdin; it is skipped
in dry-run. Do not interpolate model content into shell commands. Report templates write
under `reports/`; use the notifier to post to Slack, Notion, or ticketing systems.

`issue-fix` polls open GitHub issues for comments containing `/autofix`, `auto-fix-ci` polls
failed pull-request runs, and merge/dead-code watches poll the latest base-branch commit
via `gh`. Polling is not a webhook service and has no built-in deduplication: use external
adapter cursors or disable a completed automation to avoid duplicate work. The CI template
publishes a separate fix branch instead of pushing an existing author's PR. External-data
templates that were recurring in the source catalog use command polling on the configured
schedule. Golden-reference pattern enforcement is manual and requires an explicit reference.
Never assume a report's proposed external changes have actually been applied; that is your
notifier's responsibility. Review script permissions and report contents before enabling it.

## Parallel swarm and worktree isolation

```sh
autofix swarm fix_failing_tests lint_and_typecheck_fix --repo myrepo --agents 2
autofix swarm --automation daily-tests another-job --repo myrepo --agents 3
```

Each job gets a detached Git worktree from the same base snapshot, then its own fix branch.
With one template, `--agents N` creates N independent attempts; with several templates or
automation names, N is the concurrency limit. Automation names supply their configured params.
Worker branches commit locally only. The runner records overlapping changed files, then merges
successful worker branches in input order into `autofix/swarm-<timestamp>`. A resolver agent
handles real Git conflicts. Each integration step must pass the accumulated check commands
and unanimous multi-agent review of the integration diff. Any worker error, unresolved
conflict, failed check, or review rejection/error stops the entire swarm with a nonzero exit
status and a failure report under the log directory's `reports/`. No conflicted branch is
skipped and no partial set is published. The in-progress merge is aborted where possible;
if abort cannot safely restore the prior commit, inspect the retained worktree manually.
Swarm never pushes or opens a PR automatically, even after success. Local intermediate
branches may remain for diagnosis; a failed integration must not be published. Successful
integration branches are local and have passed the same unanimous review requirement.

A repository-level advisory lock in the common Git directory rejects other autofix runs
while a run/swarm/review owns the repository. Workers only operate on separate worktrees.
The daemon uses worktrees as well; it does not switch or dirty your original checkout.
Worktrees are kept in a sibling `.<repo>-autofix-worktrees/` directory, including failures,
for inspection. Remove them manually with `git worktree remove PATH` after saving any work;
no forced cleanup is performed. Branches remain available. Install target dependencies where
each worktree can use them; ignored build artifacts and virtual environments are not copied.
The lock coordinates autofix only, not editors, other Git clients, or shell subprocesses.
This implementation uses POSIX file locks (Linux/macOS).

## Multi-agent review and publication gate

```sh
autofix review --repo myrepo --branch autofix/some-fix --agents 3
autofix review --repo myrepo --pr 42
```

Reviewers default to correctness, security, and tests/style. Configure `review.roles` to
change the role list; `--agents N` cycles through these roles for N reviewers. Reviewers run
in parallel, then a read-only aggregator deduplicates their structured findings. They receive
only list_files, read_file, and finish: no shell, writes, Git commands, or application tests.
A separate detached worktree holds the reviewed snapshot. Branch reviews compare against
base_branch using a merge-base diff. PR reviews fetch the PR head from origin and obtain the
diff through gh; they require repo.github, a GitHub origin, and gh authentication.
Reports are saved under the effective log directory's `reports/`. PR reviews also post a PR
comment using that report; standalone branch reviews do not publish anything.

Every automatic push/PR requires **unanimous multi-agent approval** after the checked local
commit. This is mandatory: there is no opt-out. Every reviewer and the aggregator must return
valid results with `approved: true` and no blocking findings. An aggregator cannot override
a rejection or erase a reviewer's blocker. At least two reviewer roles are required for
publishing; the default is three. Invalid JSON, an oversized diff (`review.max_diff_chars`,
default 100000), a failed reviewer, or an explicit rejection fails closed. Rejection returns
`publication_blocked: true`, retains the local commit, and writes a report without pushing
or opening a PR. Local dry-runs may skip review because they cannot publish. Daemon, issue,
and CI fixes use the same mandatory gate. There is no automatic reviewer-driven fix loop.
Review is a model opinion, not a security guarantee. Even the read-only file tools can see
sensitive repository files: isolate clones.

Logs requested inside a configured repository are redirected to its common Git directory's
`autofix-logs/` (including daemon, swarm, and review logs) so they never dirty the working tree.
Other relative log paths resolve from the caller's working directory; CLI defaults resolve
from the config directory. `issue_limit` controls GitHub issue polling (default 200), and an
automation parameter of the same name overrides it. Polls beyond that bound need a larger
limit or an external paginated command adapter. Command timeouts kill the entire shell process
group; a deliberately detached subprocess is outside that group and requires OS isolation.

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
- Tool output is truncated to roughly 16K characters. Commands time out after 120 seconds by default, with a maximum of 600 seconds. max_steps limits chat rounds, not tokens or cost. There is no OS sandbox, budget control, or persistent task queue. Use swarm for concurrent jobs; independent autofix commands on a locked repository fail instead of sharing a working tree. Issues that remain open will be attempted again on the next run (there is no deduplication state).
- Execution stops at `finish` or max_steps. Runs that do not finish or fail checks are not committed/pushed. Failures preserve the current branch and files for investigation. No diff means no empty commit/PR.

## Development and testing

```sh
pip install -e '.[test]'
pytest -q
autofix templates
```

Tests use a stdlib HTTP fake model server to repair genuinely failing tests in a temporary Git repository and verify branches, commits, checks, and error boundaries. They also cover the JSON tool protocol, Tinker defaults and bearer authentication, the complete catalog, command events, locks and daemon worktrees, intentional swarm conflicts (resolved and skipped), read-only review aggregation, and blocking publication gates. No real API key is needed. Verify the actual Inkling endpoint, GitHub push/PR workflow, and launchd in your own environment.

In short: A minimal self-hosted tool-calling repair runner. Use an isolated clone, configure an OpenAI-compatible endpoint, and review generated branches and PRs. Shell access is **not sandboxed**.

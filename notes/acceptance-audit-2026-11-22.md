# Acceptance audit — 2026-11-22

## Scope and standard

Audited ACCEPTANCE.md §§1–8; root AGENTS.md, CLAUDE.md and `.cursor/rules/acceptance.mdc`; README instructions and claims; current CLI/Mac packaging and test coverage; full git history and all five GitHub PRs. No criteria added beyond those rules. No environment blueprint exists. Changes are documentation-only; no product code, dependencies, configuration or tests changed. No secret values are included in this note or the diff.

## Failing items

| Acceptance / source rule | Finding and evidence | Resolution |
|---|---|---|
| §5 — `AGENTS.md`, `ship-visible-mvp.mdc` | README omitted the explicit LIVE/BLOCKED/LOCAL ONLY status and next-human-action line. GitHub homepage empty, Pages disabled/API 404, deployments `[]`. | Fixed here: LOCAL ONLY plus validate intended connection and native Mac app action. An unlisted external deployment is not ruled out. |
| §4 — `ui-step-screenshot.mdc` | Mac UI was added by PR #2 and connection choices by PR #4; current native 390×844 screenshots and interaction walkthrough are unavailable. `uname -s` is Linux, no osascript installed. Tests explicitly stub native dialogs and credential APIs. | Not fixable on this host: need real macOS runtime for genuine UI evidence. No browser replica or simulated dialog recording presented as native proof. README now makes this gap explicit. |
| §8 — `verification.mdc`, `senior-engineer-loop.mdc` | PRs #1, #2, #3, #4 fail the current checker with `required evidence closer (one fenced txt block)`, exit 1. | Historical limitation, not retroactively fixable as merge-time compliance. Listed here; no historical PR bodies rewritten. These PRs precede acceptance-standard PR #5. |
| §2/§5/§7 — verification, production proof and blockers | Offline fake-model and stubbed-native tests do not verify real provider credentials, hosted KeyShield deployment, actual macOS/launchd behavior or GitHub publication. README already states several such limits. | Existing caveats preserved and explicit audit/status scope added. Live/native/production evidence remains unavailable in this audit; no secret requested or used. |

## Reproduction / root cause / minimal fix

README contains detailed safety and verification caveats but lacked the required single product-status/next-action line. GitHub API checks show no configured hosted target; this tool is documented as self-hosted rather than a cloud service, so LOCAL ONLY describes what was established rather than inventing a deployment. The native UI evidence gap is a platform constraint, not a diagnosed launcher defect.

To check whether Linux tests actually exercise native UI, inspected `tests/test_macos_app.py`: it builds real bundle structure, but substitutes osascript and other macOS interactions. Those passing tests cannot establish screenshots or real Keychain/dialog behavior. This disconfirms treating a green suite as native visual verification. Only documentation was changed; no speculative launcher/backend/provider fix.

## Passing items / applicability

- §1: inspect → plan → focused docs patch → actual checks. No refactor, new dependency or deleted working flow. Existing package's test extras installed into an external isolated venv only.
- §2: all 141 tests pass; CLI templates/help and actual unsigned bundle build complete. Tests use fake local services; no live model quality claim. Existing README caveats distinguish dry-run from isolation, proposal validation from executed tests, and review opinion from security guarantees.
- §3: no un-reproduced product bug patched. Status omission and historical closer failures directly observed; platform constraints documented rather than bypassed.
- §4: native macOS UI remains visually and interactively unverified. No product screenshots or WebP manufactured.
- §5: all PRs #1–#5 are MERGED and each merge SHA is an ancestor of fetched origin/main (`git merge-base --is-ancestor` exit 0 for each). Current task requests push/no PR, not merge/deploy. No production-fix claim.
- §6: repo rule files delegate to ACCEPTANCE. README developer commands run in isolated environment. Mac build works on Linux as documented, but native startup/Gatekeeper/Keychain/launchd not tested.
- §7: blockers below; no fabricated credentials or platform workaround.
- §8: PR #5 passes the checker, as does this audit note. Earlier PRs' missing current-format closers disclosed, not silently treated as passing.

## Checks and output

Environment: `python -m venv /home/ubuntu/audit-artifacts/inkling-autofix/venv`, then its Python/pip installed `-e '.[test]'` successfully. No shared environment changes.

```text
/home/ubuntu/audit-artifacts/inkling-autofix/venv/bin/python -m pytest -q
........................................................................ [ 51%]
.....................................................................    [100%]
141 passed in 88.00s (0:01:27)
pytest exit: 0

bash macos/build_app.sh
Built: /home/ubuntu/repos/inkling-autofix/dist/Inkling-Autofix-mac.zip

/home/ubuntu/audit-artifacts/inkling-autofix/venv/bin/autofix templates
39 lines written to templates.log

/home/ubuntu/audit-artifacts/inkling-autofix/venv/bin/autofix --help
usage: autofix [-h] [--config CONFIG]
               {init,templates,run,daemon,swarm,review,propose,apply} ...
Inkling repository repair runner
```

Historical checker command, for N=1 through 5:

```text
gh api repos/lilaclilac09/inkling-autofix/pulls/N --jq .body | python scripts/check_acceptance.py
PR #1: checker exit 1; ACCEPTANCE.md: required evidence closer (one fenced txt block):
PR #2: checker exit 1; ACCEPTANCE.md: required evidence closer (one fenced txt block):
PR #3: checker exit 1; ACCEPTANCE.md: required evidence closer (one fenced txt block):
PR #4: checker exit 1; ACCEPTANCE.md: required evidence closer (one fenced txt block):
PR #5: checker exit 0; ACCEPTANCE.md: evidence closer complete
```

README status/native-limitation assertion: `PASS: explicit README product status and native-verification limitation present`. Audit closer: `python scripts/check_acceptance.py notes/acceptance-audit-2026-11-22.md`. Whitespace: `git diff --check`, no diagnostics. No configured lint/typecheck developer command; no arbitrary stricter lint criterion added.

Not run: native Mac app, 390×844 native stills/walkthrough, launchd, actual provider/KeyShield endpoints or live automation publication. No secret values printed, committed, or placed into reports. Fake-service tests and package build are not proof of these unrun flows.

Artifacts under `/home/ubuntu/audit-artifacts/inkling-autofix/` (not committed):

- `pytest.log`, `build.log`, `templates.log`, `help.log`, `install.log`
- `Inkling-Autofix-mac.zip` — unsigned bundle copied from successful build, not a native-runtime verification artifact

## Blockers

| blocked_on | evidence | impact | human_action | not_doing |
|---|---|---|---|---|
| Native macOS UI/runtime evidence | Linux host; no osascript; native APIs stubbed in passing tests. | degraded: offline/package verification only | Run Mac app on macOS and capture genuine 390×844 stills plus an interaction walkthrough. | No browser mock, emulated screenshots or passing-native claim. |
| Live model/hosted integration and publication verification | Suite uses fake-model services; no live inference or publishing command run. | degraded: no production proof | Validate intended model/KeyShield connection and publication in an isolated authorized setup. | No secret-value collection/reporting, live API expenditure or test-driven publishing to unrelated repos. |
| Historical closer compliance | Checker exits 1 for PRs #1–#4; all already merged before acceptance PR #5. | degraded: historical audit finding | No action can establish missing merge-time evidence retroactively; use current closer on future work. | No rewriting old PR bodies or claiming historical compliance. |

Status: LOCAL ONLY — next human action: validate intended model connection in an isolated clone and native app on macOS.

```txt
root cause: README omitted required status/next action; historical PRs lack current-format evidence closers; Linux tests cannot supply native Mac UI proof.
files changed: README.md; notes/acceptance-audit-2026-11-22.md.
checks run: 141 tests pass; Mac bundle build, CLI templates/help and README status check pass; PR 5 closer passes, PRs 1-4 fail; audit closer passes; whitespace check clean.
manual QA: inspected CLI output and native-test boundaries; real macOS UI, mobile stills and walkthrough unavailable on Linux.
verification: logs and unsigned bundle at /home/ubuntu/audit-artifacts/inkling-autofix/; no live provider, native UI or production verification claimed.
remaining risks: historical closer gaps; native macOS/Gatekeeper/Keychain/launchd and live integration/publication unverified.
manual steps: validate native app on macOS with genuine stills/walkthrough and test intended model connection in an isolated authorized clone.
safe to merge: yes
```

# Inkling Autofix

一个小型、自托管的「Devin-like」Python 自动修复器。接入任何支持 **OpenAI Chat Completions 工具调用** 的模型（包括你的 Inkling），在本地克隆中读代码、运行命令、修改文件、检查并提交；可按时运行，配置 GitHub 后自动开 PR。Python 3.10+，不是云服务，也不需要真正的 OpenAI API key。

## MacBook 五分钟开始

先安装 Python 3.10+、Git；准备独立的干净仓库克隆、Git 提交身份，以及项目本身的测试环境。可选安装 GitHub CLI：`brew install gh && gh auth login`。

```sh
cd inkling-autofix
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
export INKLING_API_KEY='你的密钥'
autofix init
# 编辑 config.yaml：填入 base_url（通常以 /v1 结尾）、模型名、仓库绝对路径
# 配置 base_branch；如需 PR，取消 github: owner/repo 注释
# 设置 test_command 为目标项目真实可运行的测试命令
autofix templates
autofix run fix_failing_tests --repo myrepo --dry-run
# 检查分支和提交后，再启用 config.yaml 中需要的 enabled: true
autofix daemon
```

`--dry-run` **仍会修改文件、运行命令、创建分支和本地提交**，只跳过 push/PR 和 issue 评论。运行命令工作目录为目标仓库；解释器、测试工具和依赖必须事先可用。非 Python 项目可将 `test_command` 改为 `npm test` 等。

`autofix --config /absolute/path/config.yaml …` 可指定配置；相对 repo 路径相对于配置文件。`autofix init` 不覆盖已有文件。API key 只从指定的环境变量读取，不要写进配置。模型必须支持 `tools`/`tool_calls`，不支持时明确报错，不降级为解析聊天文本。

## 模板

模板源码在 `templates/`（链接到打包目录 `autofix/templates/`），使用 Python `str.format` 占位符而非 Jinja；字面花括号需写为 `{{` / `}}`。

| 模板 | 行为 | 成功检查 / 参数 |
| --- | --- | --- |
| `fix_failing_tests` | 执行测试并修复失败 | `test_command`，默认 `python -m pytest -q` |
| `fix_github_issues` | `gh issue list` 获取 open + bug 标签，每个 issue 独立分支/PR，成功后评论 PR 链接 | `test_command`；需 `github` 和 `gh` |
| `fix_ci_failure` | 读取配置 base 分支最新失败 run 的 `gh run view --log-failed` 日志并修复 | `test_command`；需 `github` 和 `gh` |
| `lint_and_typecheck_fix` | 修复 lint、类型错误后测试 | `lint_command` 默认 `ruff check . && mypy .`，以及 `test_command` |
| `dependency_update` | 小步更新过期依赖与锁文件，避免大版本升级 | `test_command` |
| `custom` | 自由文本任务 | `--task`；默认无外部检查，建议配置 `check_command` |

```sh
autofix run custom --repo myrepo --task '修复日期解析的时区错误，并添加回归测试' --dry-run
```

配置中每个 automation 可直接放额外模板参数，也可使用 `params: {test_command: "…", check_command: "…"}`。手动 run 使用模板默认参数（CLI 仅覆盖 task），若测试不是 pytest，请在对应模板内修改默认命令或使用 automation 配置运行 daemon。新增 YAML 模板需要 name、description、trigger.type、prompt，可选 check_command。`config.example.yaml` 是根目录示例，打包副本位于 `autofix/config.example.yaml`。

daemon 启动后立即执行 enabled 项，然后按 `schedule_minutes` 间隔串行调度；重启会重置计时。`logs/runs.jsonl` 保存每次 agent 运行的状态、分支、检查结果、摘要与 token usage；`logs/daemon.jsonl` 保存调度错误。不记录 API key 或完整模型请求。日志可能含代码、命令输出或 issue 内容，请保护并自行轮转。没有 issue/失败 run 时正常跳过。失败留下现场，不自动清理或丢弃变更，后续运行遇到脏工作区会拒绝执行；需要人工处理。

## Mac 后台运行（可选 launchd）

复制 `examples/com.aileen.autofix.plist` 到 `~/Library/LaunchAgents/`，把示例中所有 `/Users/aileen` 路径改为自己的绝对路径，并填写 key（文件权限设为 `600`，不要提交），或用适合自己的安全凭据注入方式。

```sh
chmod 600 ~/Library/LaunchAgents/com.aileen.autofix.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.aileen.autofix.plist
# 停止：launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.aileen.autofix.plist
```

launchd 不继承终端的 export 或虚拟环境；示例指定 venv 可执行文件、工作目录及 PATH。运行前确保日志目录存在，Git/gh 身份可用；机器休眠时不能持续执行。本示例未在 macOS 实机验证。

## 安全边界与限制（请先读）

- 只用于你信任的仓库，建议独立低权限用户、容器/虚拟机、受限网络与独立克隆，不要暴露 SSH 私钥或生产凭据。
- 每次从本地 `base_branch` 新建 `autofix/<template>-<timestamp>`。不自动 fetch/pull，请自行更新 base；要求干净工作区。不会通过 runner 的 Git 工作流推送 base 分支。成功且检查通过后才提交，配置 GitHub 且安装 gh 时才 push 和开 PR。请保留人工 review 与分支保护。
- 文件工具限制在仓库根目录并拒绝 `.git`、路径穿越与指向仓库外的符号链接。**run_command 是以仓库为 cwd 的任意 shell，不是沙箱**：模型、测试和依赖安装脚本仍能访问宿主机、网络和凭据，也可能绕过 Git 提示限制。不要把路径检查或 dry-run 当作安全隔离。
- 模型可出错；issue、CI 日志、仓库内容可能含提示注入。检查通过不代表正确，默认测试命令需按项目调整。不要让模型接触敏感文件；新生成文件可能被纳入提交，发布前审查。
- 工具输出截断至约 16K 字符，命令默认超时 120 秒，最高 600 秒；max_steps 限制聊天轮数而非 token/费用。没有 OS 沙箱、预算控制、持久任务队列、并发锁或自动冲突恢复。同一仓库不要并发运行；issue 仍 open 时下次会再次尝试（没有去重状态）。
- `finish` 或达到 max_steps 时停止；未 finish、检查失败均不提交/推送。失败时保留当前分支和文件供排查。没有 diff 时不会创建空提交/PR。

## 开发与测试

```sh
pip install -e '.[test]'
pytest -q
autofix templates
```

测试使用 stdlib HTTP 假模型服务器，在临时 Git 仓库修复真实失败测试、验证分支/提交/检查及错误边界；不需要真实 API key。真实 Inkling endpoint、GitHub push/PR 和 launchd 需在你自己的环境验证。

English: A minimal self-hosted tool-calling repair runner. Use an isolated clone, configure an OpenAI-compatible endpoint, review generated branches and PRs. Shell access is **not sandboxed**.

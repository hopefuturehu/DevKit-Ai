# bot

`bot` 是一个通用的本地优先 CLI Agent。核心通过 OpenAI-compatible API 调用模型，并以
Skill 和 Tool 扩展鲲鹏迁移、性能分析等领域能力。

文档入口见 [docs/README.md](docs/README.md)，按使用指南、架构、设计演进、评测、调研、求职资料和历史归档分类。
项目设计目标和边界见 [docs/architecture/design.md](docs/architecture/design.md)，已实现能力与待验收项见
[实现状态](docs/architecture/implementation-status.md)。

## 已实现能力

- OpenAI-compatible Chat Completions 流式文本和结构化 Tool Calling；
- 可恢复、可分叉的 SQLite 会话和 JSONL 审计事件；
- 面向使用者的 Web 实时任务工作台：工具参数与日志、运行中补充要求和停止、历史恢复、
  文件差异与下载、子任务时间线和上下文查看；
- 模型自主维护的会话级 TODO list，支持严格快照校验、事件持久化、CLI/Web 实时展示和
  上下文压缩后恢复；
- read/search/apply-patch/argv command/受控 shell/network 等通用 Tool；长命令在短暂
  同步等待后转为可轮询、可输入、可终止的受管进程；
- Workspace 路径边界、危险操作审批、审批作用域、输出截断和敏感值脱敏；
- 指定目录 Skill 的三段式披露、自动或 `$skill-name` 显式激活、Run 隔离、历史正文交付与压缩恢复；
- KSYS 与 DevKit Tuner 的结构化 Subprocess CLI Adapter；
- x86 或缺少工具时生成鲲鹏 ARM 手动执行命令并接受后续自由文本结果；
- 运行中 steering、`/cancel`、五级上下文管理、可恢复单摘要压缩、显式/自动 Markdown
  长期记忆和 Token/费用记录；
- 压缩事务发布、完整 Transcript 保留、覆盖范围与来源哈希校验、失败不推进、摘要回滚与原文重建；
- Markdown 自定义 Agent catalog，支持前台/后台 `task`、同 child session 多轮续接、
  持久化 mailbox、`waiting_parent` 提问、运行轨迹与用量审计；`coder` 在独立
  Git worktree 中修改，可校验采用 patch 并清理 worktree。

## 开发安装

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

使用官方 DeepSeek V4 Flash 时，一次性安装对应 tokenizer，以启用模型输入计数和预算余量：

```bash
.venv/bin/python -m bot.providers.install_tokenizer
```

正常请求不会下载词表；计数口径及验证结果见[输入 token 校准](docs/architecture/input-token-calibration.md)。

Web 工作台使用相同的模型配置与工作区：

```bash
.venv/bin/pip install -e '.[web]'
.venv/bin/bot -C . web start --host 127.0.0.1 --port 8080
```

打开 `http://127.0.0.1:8080`。使用方法和状态说明见 [Web 任务工作台](docs/guides/web-workbench.md)。

## 最小配置

在工作区创建 `.bot/config.toml`：

```toml
[model]
provider = "openai_compatible"
base_url = "https://your-provider.example/v1"
# 可选：直接保存在工作区 TOML；该文件必须保持 0600 且不得提交
# api_key = "your-api-key"
api_key_ref = "auto:BOT_MODEL_API_KEY"
name = "your-model-id"
context_window_tokens = 131072
# 可选；配置后才能计算并限制费用
input_cost_per_million = 0.0
output_cost_per_million = 0.0

[agent]
max_tool_output_bytes = 1000000
model_request_retries = 2
model_request_retry_backoff_seconds = 1
process_wait_seconds = 10
max_managed_processes = 16
# max_cost_usd = 2.0
# 兼容/部署策略：仅在确实需要硬预算时显式配置
# max_steps = 100
# max_wall_time_seconds = 7200
# max_total_tool_output_bytes = 50000000
# max_consecutive_failures = 10
# process_hard_timeout_seconds = 14400

[agent.progress]
enabled = true
warning_after_no_progress_steps = 4
recovery_after_no_progress_steps = 7
finalize_after_no_progress_steps = 11
max_recovery_attempts_per_epoch = 1
process_inactivity_warning_seconds = 300
process_inactivity_recovery_seconds = 900
# 默认不因静默自动终止仍存活的进程；需要时显式设置
# process_inactivity_finalize_seconds = 3600

[agent.finalization]
enabled = true
model_timeout_seconds = 120

[subagents]
enabled = true
max_concurrent = 3
max_queued = 32
max_tasks_per_session = 16
allow_worktree_writes = true
# max_cost_usd_per_task = 0.5
# max_total_cost_usd_per_session = 2.0

[agents]
user_path = "~/.bot/agents"
project_path = ".bot/agents"
auto_resume_background = false
required_wait_timeout_seconds = 900

[permissions]
mode = "safe"
# 危险选项：自动批准所有 ASK；敏感路径、越界路径和非法策略绕过仍会拒绝
# auto_approve = false
workspace_only = true
network = "ask"

[context]
compaction_strategy = "a_fallback"
max_input_tokens = 120000
auto_compact_threshold = 0.8
output_reserve_tokens = 4096
protocol_reserve_tokens = 2048
safety_margin_tokens = 2048
# 仅旧 current/B 策略使用；双路径始终使用当前主模型
# compaction_model = "<summary-model-id>"
# 连续 raw tail 按 token 有界；强制恢复收集到三条 user 即停，也不加入会使 tail 超预算的更旧组
recent_conversation_tokens = 20000
compaction_min_recent_user_turns = 3
memory_tokens = 8000
active_skill_tokens = 16000
tool_schema_tokens = 16000
tool_result_inline_tokens = 4000
tool_result_head_chars = 6000
tool_result_tail_chars = 2000
# 摘要长度软目标；完整摘要不会仅因超过该目标而被拒绝
compaction_summary_target_tokens = 3000
compaction_max_output_tokens = 8192
compaction_max_input_tokens = 60000
compaction_input_target_ratio = 0.8
compaction_repair_attempts = 1
compaction_condense_attempts = 1
compaction_empty_retries = 1
compaction_transport_retries = 1
compaction_transport_retry_backoff_seconds = 1
compaction_range_attempts = 2
compaction_failure_backoff_seconds = 300
compaction_request_timeout_seconds = 90
compaction_command_max_requests = 8
compaction_command_max_seconds = 600
compaction_command_max_cost_usd = 0.25
compaction_source_refs = "range"
# 双路径两次摘要的 thinking 都跟随 model.thinking；未设置时沿用供应商默认值
# 旧 compaction_thinking 配置可继续加载，但不会覆盖双路径的主请求设置
compaction_max_message_chars = 12000
compaction_rebuild_every = 5

[memory]
enabled = true
path = "./.bot/memory"
auto_extract = true
context_mode = "on_demand"
# 可选；留空时复用主模型
# model = "<memory-extraction-model-id>"
max_runs_per_cycle = 3
max_candidates_per_run = 5
min_confidence = 0.75
index_tokens = 2000
router_enabled = true
router_enforce_required = true

[skills]
path = "./skills"
context_mode = "history"

[storage]
# 相对路径以工作区为基准
state_path = "./.bot/state.db"
```

`model_request_retries` 只覆盖 Provider 标记为可重试的限流、服务端、超时和传输错误；默认最多
重试两次，退避等待最多 30 秒。认证、付费、配置、协议和上下文超限不会走这条路径。未完成的
流式正文和 Tool Call 会被丢弃，但 Provider 已报告的 token/费用仍累计。

正常任务默认不受固定步骤数或总运行时长限制。Tool 通过结构化 `progress` 信号报告强/弱进展
或外部等待；运行器识别重复失败和短周期循环，进展状态会写入 SQLite 并在 `resume` 时恢复。
所有非取消终态统一生成一次无 Tool 或静态收尾。完整状态机见
[docs/architecture/termination.md](docs/architecture/termination.md)。

复制示例环境文件并填写模型 API Key（源码仓库和 `bot init` 创建的工作区均包含
`.env.example`；`.env` 已加入 `.gitignore`，不会被 Git 提交）：

```bash
cp .env.example .env
```

```dotenv
BOT_MODEL_API_KEY=your-api-key
```

然后运行：

```bash
bot doctor
bot
```

如需在受控环境中完全跳过 Human Approval，可在配置中显式设置：

```toml
[permissions]
mode = "full-access"
auto_approve = true
network = "allow"
```

也可以在启动时使用 `BOT_AUTO_APPROVE=true bot`。该选项会自动批准所有原本为 `ASK` 的
Tool 请求，但仍保留敏感路径、工作区边界、非法参数和策略绕过等 `DENY` 规则。

`auto:<VARIABLE>` 优先读取已有环境变量，未设置时回退到工作区根目录的 `.env`，且不会修改
进程环境。需要固定来源时，可分别使用严格的 `env:<VARIABLE>` 或 `dotenv:<VARIABLE>`。
也可以通过 `model.api_key` 直接提供凭据；直接值优先于 `api_key_ref`，`bot config get` 只显示
`<redacted>`。删除或清空直接值后，仍会按 `api_key_ref` 从环境变量或 `.env` 读取。工作区
`.bot/` 默认被 Git 忽略，但保存凭据的配置文件仍应设置为 `chmod 600 .bot/config.toml`。

`bot init` 会创建项目配置和不含真实密钥的 `.env.example`，并在目标 Skill 不存在时把随包发布的
`kunpeng-performance-analysis` 脚手架到工作区 `./skills`；已有同名目录不会被覆盖。

也可以执行单次任务：

```bash
bot run "分析这个项目"
bot run --json "列出当前项目结构"
```

常用管理命令：

```bash
bot doctor
bot config get
bot config set model.name '<model-id>'
bot model list
bot model set '<model-id>'
bot skill list
bot skill show kunpeng-performance-analysis
bot session list
bot session show '<session-id>'
bot session fork '<session-id>'
bot resume '<session-id>'
```

运行可复现的 JSONL 评测集，并保存逐 Case 指标：

```bash
bot eval run evals/generic.jsonl -o .bot/eval-generic.jsonl
bot eval run evals/artifacts.jsonl -o .bot/eval-artifacts.jsonl
bot eval run evals/kunpeng.jsonl -o .bot/eval-kunpeng-with-skills.jsonl
bot eval run evals/kunpeng.jsonl --disable-skills \
  -o .bot/eval-kunpeng-without-skills.jsonl
```

每个 Case 在独立的 fixture 副本和状态库中运行，可验收新产物、JSON Schema、禁止修改、
真实 Tool 执行结果，以及独立容器中的测试。结果区分运行状态与验收结论，保存失败类别、
版本/输入哈希、轨迹和用量指标。字段、隔离边界与验收器协议见[通用任务评测](docs/evaluations/evaluation.md)。

通过 Harbor 在 Terminal-Bench 2.1 的一次性任务容器中运行 smoke test：

```bash
.venv/bin/python scripts/run_terminalbench.py \
  --task openssl-selfsigned-cert
```

适配器会构建本项目 wheel、注入任务容器并保存完整诊断产物。全量运行、预算、并发、
结果目录和 ARM64 限制见 [Terminal-Bench 2.1 评测文档](docs/evaluations/terminalbench-evaluation.md)。

运行结束后，一键归档最新 Job 并生成检测汇总：

```bash
.venv/bin/python scripts/archive_terminalbench_result.py
```

交互会话中可用 `/status`、`/tools`、`/todo`、`/agents`、`/skills`、`/model`、`/permissions`、
`/compact`、`/compact rebuild`、`/compact rollback <id>`、`/remember`、`/memories`、
`/forget <id-or-key>`、`/memory extract [run-id]`、`/new` 和 `/exit`。`/compact` 会在
Tool 原子组边界生成一个活动摘要，
并以事务方式推进游标。原始消息不会因压缩而删除，模型可通过
`search_session_history` 和 `load_compaction_source` 检索、回溯。完整设计见
[可恢复的单摘要上下文压缩](docs/architecture/recoverable-context-compaction.md)，故障回放与真实 Provider
门禁见[上下文压缩有效性评测](docs/evaluations/context-compaction-effectiveness.md)，与本地 Codex、OpenCode、
Pi、Hermes Agent、DeepSeek Harness、Nanobot 的架构差异见
[开源 Agent 框架上下文管理对比](docs/research/context-framework-comparison.md)。

会话和证据继续保存在 SQLite；显式记忆写入受保护的 `USER.md` 并以 `USER` 信任加载，
已完成 Root Run 会在后续运行开始时异步提取为 Markdown 自动记忆。自动记忆不需要逐条
审核，但默认不再 eager 注入：Memory Router 只在当前真实用户轮次需要历史时建议或强制
`search_memory`，涉及用户历史归因时继续强制 `load_memory_evidence`。检索正文按一次性 Tool
Result 交付，冲突不会静默覆盖。详见 [Markdown 长期记忆](docs/architecture/markdown-memory.md)。
Router 的可证伪门禁、真实 DeepSeek 成对 A/B 结果和适用边界见
[Memory Router 设计与验收](docs/architecture/memory-routing.md)。

Agent 运行期间输入的普通文本会作为 steering 在下一个安全边界生效；输入 `/cancel`
可取消当前运行。

父 Agent 首选通过 `task` 以前台或后台方式调用 Markdown 自定义 Agent，并可以使用
`send_task_message`、`get_agent_status`、`await_agents`、`cancel_agent`、`apply_agent_patch`
和 `cleanup_agent_worktree`。完整定义格式、信任模型和父子交互协议见
[Markdown 自定义 Agent](docs/guides/custom-agents.md)。
子 Agent 使用独立会话、Runner、Skill 激活状态和 Tool allowlist，默认最大委托深度为 1；
父会话历史不会被复制，只有任务目标和显式授权的 `context_ref` 会进入子会话。`required=true`
的任务会在父 Agent 最终回答前有界等待并作为不可信结构化结果回流；工作区内的
SQLite/WAL 状态文件也不对 child Tool 开放。`coder` 的 worktree 从显式
`base_ref`（默认 `HEAD`）创建，
不会自动携带主工作区未提交的改动。一次性 `bot run` 不会把任务变成进程外
daemon；父运行结束后仍未完成的 detached 任务会在 Runtime 关闭时中断。

## Skill 目录

默认扫描工作区 `./skills` 的直接子目录。仓库内提供首个领域 Skill：
`kunpeng-performance-analysis`。Skill 可以使用 KSYS 做广泛诊断，再根据证据选择 Tuner
的 `top-down`、`hotspot`、`miss`、`numafast`、`hpc-perf` 或 `roofline`，但不强制固定流程。

Skill 激活只对当前 Run 生效：一次用户请求及其后续模型、工具和收尾步骤共用绑定，Run 结束后
释放。下一 Run 使用同一 Skill 时需重新选择；已在历史中完整保留的同版正文可以复用。
新会话默认把自动加载正文放入 Tool Result，把显式加载正文放在真实用户消息之后，不再使用
独立 Active Skill 前置层。压缩覆盖必要正文时，Runtime 在继续执行前恢复原版本并验证完整性。

升级前已有会话保留 `legacy` 布局；新会话首次运行按 `skills.context_mode` 选定模式并持久化，
修改配置不会切换已有会话，fork 继承原模式。默认配置下用 `/new` 开始历史交付模式。
`/skills reload` 重新扫描目录，正在执行的 Run 继续使用启动时的 Catalog 快照。
实际布局和当前绑定可在 `/status` 的 `context.skill_context_mode`、`active_skills` 查看。
实现、迁移与验证结果见 [Skill 历史交付实施与验证](docs/evaluations/skill-context-validation.md)。

## 验证

```bash
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/pytest -q
.venv/bin/pip check
```

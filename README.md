# bot

`bot` 是一个通用的本地优先 CLI Agent。核心通过 OpenAI-compatible API 调用模型，并以
Skill 和 Tool 扩展鲲鹏迁移、性能分析等领域能力。

当前实现目标和边界见 [docs/design.md](docs/design.md)。

## 已实现能力

- OpenAI-compatible Chat Completions 流式文本和结构化 Tool Calling；
- 可恢复、可分叉的 SQLite 会话和 JSONL 审计事件；
- read/search/apply-patch/argv command/受控 shell/network 等通用 Tool；
- Workspace 路径边界、危险操作审批、审批作用域、输出截断和敏感值脱敏；
- 指定目录 Skill 的三段式披露、自动或 `$skill-name` 显式激活、资源按需读取；
- KSYS 与 DevKit Tuner 的结构化 Subprocess CLI Adapter；
- x86 或缺少工具时生成鲲鹏 ARM 手动执行命令并接受后续自由文本结果；
- 运行中 steering、`/cancel`、五级上下文管理、结构化快照恢复、显式长期记忆和
  Token/费用记录；
- 持久化后台子 Agent Worker Pool：`explorer`/`reviewer` 只读并行调查，`coder`
  在独立 Git worktree 中修改；支持状态查询、等待、取消、崩溃后 fail-closed 恢复和
  required 结果自动汇合。

## 开发安装

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

## 最小配置

在工作区创建 `.bot/config.toml`：

```toml
[model]
provider = "openai_compatible"
base_url = "https://your-provider.example/v1"
api_key_ref = "env:BOT_MODEL_API_KEY"
name = "your-model-id"
context_window_tokens = 131072
# 可选；配置后才能计算并限制费用
input_cost_per_million = 0.0
output_cost_per_million = 0.0

[agent]
max_steps = 30
max_wall_time_seconds = 1800
max_tool_output_bytes = 1000000
max_total_tool_output_bytes = 5000000
# max_cost_usd = 2.0

[subagents]
enabled = true
max_concurrent = 3
max_queued = 32
max_tasks_per_session = 16
max_steps = 15
max_wall_time_seconds = 900
allow_worktree_writes = true
# max_cost_usd_per_task = 0.5
# max_total_cost_usd_per_session = 2.0

[context]
max_input_tokens = 120000
auto_compact_threshold = 0.8
output_reserve_tokens = 4096
protocol_reserve_tokens = 2048
safety_margin_tokens = 2048

[skills]
path = "./skills"

[storage]
# 相对路径以工作区为基准
state_path = "./.bot/state.db"
```

然后运行：

```bash
export BOT_MODEL_API_KEY='...'
bot doctor
bot
```

`bot init` 会创建项目配置，并在目标 Skill 不存在时把随包发布的
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
bot eval run evals/kunpeng.jsonl -o .bot/eval-kunpeng-with-skills.jsonl
bot eval run evals/kunpeng.jsonl --disable-skills \
  -o .bot/eval-kunpeng-without-skills.jsonl
```

Case 可断言最终状态、答案片段、工作区文件、Tool/Skill 轨迹和审批次数；结果记录耗时、
Token、费用、Tool Call 和激活 Skill，便于比较通用 Agent 与领域 Skill 的增益。

交互会话中可用 `/status`、`/tools`、`/agents`、`/skills`、`/model`、`/permissions`、
`/compact`、`/remember`、`/memories`、`/new` 和 `/exit`。Agent 运行期间输入的普通文本会
作为 steering 在下一个安全边界生效；输入 `/cancel` 可取消当前运行。

父 Agent 可调用 `spawn_agent`、`get_agent_status`、`await_agents` 和 `cancel_agent`。
子 Agent 使用独立会话、Runner、Skill 激活状态和 Tool allowlist，默认最大委托深度为 1；
父会话历史不会被复制，只有任务目标和显式授权的 `context_ref` 会进入子会话。`required=true`
的任务会在父 Agent 最终回答前自动等待并作为不可信结构化结果回流；工作区内的
SQLite/WAL 状态文件也不对 child Tool 开放。`coder` 的 worktree 从当前 `HEAD` 创建，
不会自动携带主工作区未提交的改动。一次性 `bot run` 不会把任务变成进程外
daemon；父运行结束后仍未完成的 detached 任务会在 Runtime 关闭时中断。

## Skill 目录

默认扫描工作区 `./skills` 的直接子目录。仓库内提供首个领域 Skill：
`kunpeng-performance-analysis`。Skill 可以使用 KSYS 做广泛诊断，再根据证据选择 Tuner
的 `top-down`、`hotspot`、`miss`、`numafast`、`hpc-perf` 或 `roofline`，但不强制固定流程。

## 验证

```bash
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/pytest -q
.venv/bin/pip check
```

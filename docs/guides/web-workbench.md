# Web 任务工作台

Web 工作台用于在 Bot 执行任务时查看进展和结果，与 CLI 共用模型配置、工具、审批策略和持久化会话。

## 启动

```bash
.venv/bin/pip install -e '.[web]'
.venv/bin/bot -C /path/to/workspace web start --host 127.0.0.1 --port 8080
```

浏览器打开 `http://127.0.0.1:8080`。前端随 Python 包提供，无需 Node 构建。
从其他设备访问时，使用同一服务的可达地址和原会话；本工作台没有多用户身份系统，工作区范围校验不等于账户隔离。

## 使用

- 左侧新建或选择会话，顶部选择历史任务。会话、任务列表可继续加载更早记录。
- 中间展示实际计划、Bot 流式正文与工具卡。点击工具后，在右侧切换输出、完整参数和原始调用事件。
- 运行中可以发送补充要求；先显示等待接收，执行端确认后标为已接收。失败重试保留同一请求 ID，不重复启动任务。
- 停止任务会取消当前 Run、它启动的受管进程和所属子任务，保留已经产生的输出。页面先显示“正在停止”，收到终态后才显示“已停止”。
- 文件页显示运行前后真实工作区变化，提供 diff、文本/图片预览和快照下载。子任务页显示职责、约束、状态和共同时间轴，点击可查看子任务独立轨迹。
- 上下文页显示累计用量、上下文/记忆事件、已保存的压缩摘要和来源消息。完整事件日志可分页读取；只展示实际记录的 reasoning。
- “历史分析”页跨会话汇总主会话库、评测库与归档产物中的运行，可按状态、来源、停止原因、会话、关键字和时间范围筛选，按净耗时等字段排序，并查看停止原因分布。点击运行查看耗时构成，或直接跳转到该运行的执行轨迹。没有证据的时间空档标为“未知”，不当作已知耗时。
- 支持浅色、深色和跟随系统。窄屏通过左侧菜单与详情抽屉操作；`Enter` 发送、`Shift+Enter` 换行、`Esc` 关闭详情并返回原工具。

向上滚动会暂停自动跟随，点击“回到最新”恢复。主对话默认显示最近 150 个节点；展开历史不会更改执行。工具和进程日志各流保留最近 64,000 个字符用于实时显示，更早的持久化记录可分页读取。

## 历史任务分析台

“历史分析”页读取当前工作区下发现的所有 bot 历史数据库，跨会话列出运行，用于回答“时间花在哪里、为什么停下”。统计范围限当前工作区，不含子任务运行。

### 数据来源与去重

分析台自动发现三类来源，无需手工导入：

| 来源 | 位置 | 说明 |
| --- | --- | --- |
| `main` | `.bot/state.db` | 主会话库，当前工作区的实时运行记录 |
| `benchmark` | `.bot/benchmarks/**/state.db` | 评测运行库 |
| `artifact` | `artifacts/**/state.db` | 归档产物中的运行库 |

- 所有来源都以**只读连接**（`file:...?mode=ro`）打开，不迁移、不修改源库；采集前后源库字节不变。
- 同一 Run 可能同时出现在 `agent/state.db`、`trace/state.db` 或快照中。去重按 **Run ID + 内容指纹**（`runs` 行与事件流哈希）判断，**不按文件名**判断。
- 内容完全相同的副本记为重复，只统计一次；内容不同的副本记为**冲突**，在界面上可见提示，并保留全部来源路径。
- 选取优先级：`main` > `benchmark` > `artifact`；同级取事件数多者，再按路径排序，保证结果确定可复现。
- 单个数据库损坏或 schema 不兼容时，只跳过该库并在界面列出原因，**不影响其他来源**。
- 来源筛选可只看某一类来源；导出报告包含来源、去重信息与统计口径。

### 统计口径

- **总耗时** = `runs.completed_at − runs.started_at`。缺少结束时间时记为**未知**，不按 0 处理。
- **审批等待** = 每个 `approval.requested` 到其配对 `approval.resolved` 的区间并集。重叠或相邻区间合并，同一秒不重复扣除。
- **审批配对**限定在同一 Run 内：优先用 `approval_id`，缺失时回退 `tool_call_id`（历史记录普遍只有后者）。两侧标识种类不相交时判为未配对，不猜测配对。
- **净耗时** = 总耗时 − 审批等待并集，下限为 0。
- **未配对审批**（没有结果、结果早于请求、或标识无法对应）只计数、不扣除，等待时长记为未知而非 0。
- **重复审批事件**按配对键去重，同一审批只计一次。
- **长时间无事件区间**（默认 ≥ 120 秒，可用 `gap_threshold_seconds` 调整）标记为“未知 · 未扣除”。事件缺失不等于没有工作发生，因此不从净耗时中扣除。
- **停止原因**按兼容新旧记录的规则判定，并保留原始状态、原因与判定依据：
  1. 具体终态事件 `run.cancelled`/`run.blocked`/`run.limit_reached` 优先；
  2. `run.completed` 或 `run.finished(status=completed)` 判为 `completed`；
  3. `runs.status` 为终态且非 `failed` 时压过旧式 `run.failed` 事件（旧终态事件类型仍保留展示）；
  4. 都没有则记 `unknown`。
  冲突时**不静默覆盖**：`stop_reason`、`stop_event_type` 与 `termination_reason` 分别保留，界面同时展示。
- **验收状态**：`status=completed` 不等于官方验收通过。没有验收证据的运行一律标为**未核验**。

界面在分析台顶部展示同一份口径说明，导出报告也随附 `methodology` 字段，避免统计结果脱离定义被引用。

### 使用

- 筛选：状态、来源、停止原因、会话、关键字、起止时间；排序：净耗时、总耗时、审批等待、开始时间、事件数，未知值在升序和降序下都排在末尾。
- **排序作用于全部筛选结果，再分页**，因此较早的长任务不会被分页窗口漏掉；相同值以 `run_id` 稳定排序，跨页无遗漏、无重复。
- 汇总统计、列表与 JSON 导出使用同一筛选口径，均覆盖**全部筛选结果**（`summary_scope: all_filtered`），不受当前页影响。
- 点击运行查看耗时构成（总耗时、审批等待、净耗时、事件数、审批明细、空档与未配对审批提示），或点击“查看执行轨迹”跳转到该运行的执行过程页；目标运行属于其他会话时会先切换会话再加载轨迹。
- 勾选两个运行可对比总耗时、净耗时、审批等待和事件数差值。
- “导出 JSON”下载 `GET /api/analysis/export` 生成的附件，schema 为 `bot.run-analysis.v1`，包含筛选条件、来源与去重信息、汇总、逐运行分析与统计口径。

### 已知限制

- 子任务运行不计入；跨工作区不汇总。
- 缺少结束时间的运行净耗时未知，不参与数值排序，只排在末尾。
- 空档提示基于事件时间戳，事件缺失或时钟偏移会放大空档；提示不改变净耗时。
- 对比面板只保留最近勾选的两个运行。
- 审批等待区间不裁剪到运行起止窗口：等待从请求时刻起算，仅净耗时下限为 0。
- 首次加载需扫描全部来源数据库，冷启动约 15 秒（本工作区 345 个来源、约 57 万条事件）；结果按进程缓存，后续筛选约 3–4 秒。
- 损坏或 schema 不兼容的数据库会被跳过并在界面提示，其运行不参与统计。

## 状态与恢复

连接状态和任务状态分别显示。刷新、关闭网页、切换会话不会停止应用持有的任务。重新连接时，服务端先发送持久化快照，再发送缓冲的实时事件；历史和实时共用事件归并器，并按事件 ID 去重。

工具调用归并键为 `(session_id, run_id, tool_call_id)`。内部工具和拒绝执行的调用可以直接由 requested/result 结束。后台命令的启动调用返回后仍显示“进程运行中”，后续轮询与独立日志观察按 `process_id` 关联；调用耗时、开始前等待和进程总耗时分别显示。

服务器重启会更换事件 epoch，客户端重新恢复历史。重启前未持久化终态的任务显示历史状态未确认；不会声称进程仍在运行，也不会自动重跑任务。审批仅对当前执行端仍持有的准确请求有效。恢复会话不代表恢复已经结束的服务器进程。

Skill 激活状态也只属于当前 Run，完成或取消后释放，历史激活事件不代表当前仍有绑定。
新会话首次 Run 按 `skills.context_mode` 选定并持久化布局，默认 history；升级前已有会话保持
legacy。重启不改变已有布局，恢复历史也不恢复已结束 Run 的活动集合。

`model.usage` 为累计值，界面直接覆盖，避免重复相加。Run 用量包含本 Run 的上下文整理，不包含子任务；子任务有独立 Run。费用缺失时显示“未记录”。源输出被截断时，已留存全文也未必能恢复最初的所有内容，详情明确提示截断或全文不可用。

## 文件产物边界

快照保存在状态数据库旁的 `web-artifacts/`，按内容寻址保存不可变内容。运行前、工具结果后及运行结束时读取实际文件；不会将 `apply_patch` 参数当成最终 diff。并发修改同一工作区时，列表表示运行期间工作区变化，不作作者归因。

每个文件上限 5 MB，快照读取上限约 100 MB、20,000 个文件。敏感路径、环境文件、密钥文件、运行状态目录和符号链接不会作为可下载内容。超限或读取失败会显式提示；不完整快照不会把未读取文件误判为删除。文本预览和下载使用脱敏版本。历史 Run 没有初始快照时不补造产物。

## 接口

| 接口 | 用途 |
|---|---|
| `GET /api/status?session_id={sid}` | Runtime 配置与指定会话当前 Run 的 `active_skills` |
| `GET/POST /api/sessions` | 会话列表/创建 |
| `GET /api/sessions/{sid}` | 会话详情、消息、用量及持久 `skill_context_mode` |
| `GET/POST /api/sessions/{sid}/runs` | 运行列表/启动；启动需要 `prompt`、`request_id`，可传 `skills` |
| `GET /api/runs/{rid}` | 运行状态、累计用量、当前执行端是否可控制 |
| `GET /api/sessions/{sid}/events` | 会话与实际子任务事件 |
| `GET /api/runs/{rid}/events` | Run 事件，支持 `children=false` |
| `GET /api/runs/{rid}/tools/{call_id}` | 聚合调用，输出按 `offset`/`limit` 读取 |
| `GET /api/runs/{rid}/tools/{call_id}/events` | 包含流式碎片的调用事件分页 |
| `GET /api/runs/{rid}/blobs/{blob_id}` | 校验归属后读取留存输出 |
| `POST /api/runs/{rid}/steer` | `text`、`message_id`，持久化补充要求并排队 |
| `POST /api/runs/{rid}/cancel` | 取消当前执行端持有的运行 |
| `GET /api/sessions/{sid}/approvals` | 当前仍可响应的审批 |
| `POST /api/approvals/{id}` | `session_id`、`run_id`、布尔值 `approved` |
| `GET /api/runs/{rid}/children` | 实际子任务与各自 Run |
| `GET /api/runs/{rid}/processes` | 该 Run 所属的当前受管进程 |
| `GET /api/runs/{rid}/context` | 最近上下文事件与压缩版本 |
| `POST /api/sessions/{sid}/compact` | 整理上下文 |
| `GET /api/sessions/{sid}/compactions/{id}` | 摘要与分页来源消息 |
| `GET /api/runs/{rid}/artifacts` | 真实文件变化列表 |
| `GET /api/runs/{rid}/artifacts/{id}` | 差异详情 |
| `GET /api/runs/{rid}/artifacts/{id}/content` | 快照内容，支持 `side=before/after` 和 `download=true` |
| `GET /api/analysis/runs` | 跨来源运行分析列表，支持 `status`/`stop_reason`/`session_id`/`search`/`since`/`until`/`source_kind`/`sort`/`order`/`limit`/`offset`/`gap_threshold_seconds`；先全量排序再分页 |
| `GET /api/analysis/runs/{rid}` | 单个运行的耗时构成、审批明细、空档与未配对审批（跨来源查找） |
| `GET /api/analysis/compare?left={rid}&right={rid}` | 两次运行的总耗时/净耗时/审批等待/事件数差值 |
| `GET /api/analysis/export` | 按当前筛选导出 JSON 分析报告（附件，schema `bot.run-analysis.v1`，含来源与去重信息） |

`/api/status` 不传 `session_id` 时，`active_skills` 是共享主 Runner 当前所有活动 Run 的名称
合集，不是某个会话的状态，也不汇总独立 child Runner。布局应读取会话详情的
`skill_context_mode`；新建且尚未首次运行的会话该字段可以为 `null`。这个字段不在
`/api/status` 的响应中。Skill 正文交付与恢复边界见[实施与验证](../evaluations/skill-context-validation.md)。

事件分页接受 `cursor`、`through`、`limit`，返回 `events/cursor/through/has_more/epoch`。第一次请求确定快照边界，后续分页沿用 `through`；游标过期返回 409。列表使用 `limit/offset`，文本使用字符偏移，不会截断 UTF-8 中文字符。

WebSocket `/ws` 的订阅请求：

```json
{"type":"subscribe","session_id":"…","subscription_id":"浏览器生成的唯一值","cursor":null}
```

可附加 `run_id` 限定范围。服务端依次返回 `snapshot`、`snapshot_complete`，随后以 `event` 包装实时记录，均附带订阅 ID。队列溢出发送 `resync_required`，客户端使用上次完整接收的游标重新订阅。任务启动与审批使用上表 REST 接口；旧版按 FIFO 处理审批的 WebSocket 消息不再接受。

## 验证

```bash
.venv/bin/pytest -o addopts='' -q
npm ci
npm run test:web
npx playwright install chromium
npm run test:web:browser
```

Python 测试覆盖实际 Runner、工具、应用生命周期、断线恢复、幂等、停止所属进程、范围校验、游标、文件快照与截断。Node 测试验证同一事件归并器的去重、累计用量、拒绝与取消、后台进程和有界日志。Playwright 启动实际 Web 服务、Runner 和本地工具，仅模型提供方使用确定性替身，无外部模型费用；覆盖历史详情、审批恢复、长任务刷新/补充/停止、滚动位置和浅深色响应式布局。

设计来源：[Web 实时任务工作台设计](../designs/web-execution-workbench-design.md)。

2026-09-08 本地验收：全项目 Python 回归 351 通过、10 跳过；事件归并测试 6 通过；真实浏览器测试 10 通过。已检查 360/736/1024 px 浅深色页面截图、代码静态检查和 wheel 内全部五个前端文件。浏览器测试不替代外部模型服务或专用硬件环境验收。

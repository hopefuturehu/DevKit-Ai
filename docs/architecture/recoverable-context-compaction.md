# 可恢复的单摘要上下文压缩

> 状态：当前实现说明
>
> 压缩基线核对：2026-08-31；2026-09-10 补充 Skill 历史交付与恢复边界；
> 2026-09-17 区分默认双路径与旧 CURRENT，并同步第三版提示、正文软目标和 thinking 继承。

## 目标

运行时上下文压缩采用单摘要模型：任意时刻只向主模型注入一组原始 user 锚点和一个活动
Assistant 摘要，摘要之后拼接未覆盖的原始消息尾部。与一次性压缩不同，本实现保留完整
Transcript，并为每个摘要记录来源范围、来源哈希、锚点、父版本和发布状态。

运行时视图为：

```text
Core / Project / Environment / Skill Catalog / Tool Catalog / USER.md
                  +
   被压缩范围的原始 user 锚点
                  +
 一个 Assistant Active Compaction
                  +
        cursor 之后的历史（含 Skill 正文，必要时追加恢复消息）
                  +
       Memory Router / Runtime Note；按需自动记忆作为 Tool Result
```

上图按新会话默认 `skills.context_mode="history"` 描述；旧 `legacy` 会话仍保留独立 Active
Skill 层。Skill 绑定属于当前 Run，压缩不会关闭它；Runtime 在下一次执行前从绑定的原文 blob
恢复被覆盖的必要正文，并检查最终 Provider 请求。合成正文不作为新的真实用户锚点。

旧的 `ContextSnapshot` 数据仍可读取，但不参与默认运行时上下文装配。已有数据库中曾由
旧版本创建的语义记忆表不会在升级时被破坏性删除，新版本也不再访问这些表。

## 四个不变量

1. **事务发布**：发布记录经 `building` 状态切换。LLM 输出通过章节、来源范围和策略要求的
   预算校验后，才在一个 SQLite 事务里把旧 `ready` 改为 `superseded`、新记录改为
   `ready`。
2. **原始记录不删除**：压缩只推进派生视图的 `cursor`，不删除或改写 `messages`、
   Tool Run 和事件。
3. **来源可验证**：每个活动摘要保存连续覆盖范围、该范围消息的 SHA-256 和活动用户锚点。
   默认 `range` 模式下 `source_refs_json` 允许为空，不要求正文逐条引用；`item` 兼容模式才保存并
   校验摘要中的 `[m:N]` 引用。加载和恢复时按覆盖范围重新读取原文并计算哈希。
4. **失败不推进**：超时、Provider 错误、缺章节、越界引用或候选预算校验失败都不会发布。
   已创建的 `building` 标记为 `failed`；默认双路径在生成通过后才创建发布记录，更早失败
   只留下事件/请求审计。旧活动摘要和游标保持不变。

## 数据与状态

`context_compactions` 保存所有版本：

- `covered_start_position` / `covered_end_position`：摘要覆盖的完整原始范围；
- `delta_start_position`：本次送给 LLM 的新增原始范围起点；
- `parent_id`：生成本版本时使用的活动摘要；
- `source_sha256`：覆盖范围内完整 SQLite 消息的来源证明，包含 `reasoning_content`；它校验
  消息内的 `context_ref`，不会展开后再重复哈希 blob 正文，blob 自身另以内容 SHA-256 寻址；
- `source_refs_json`：只保存摘要正文实际出现的 `[m:N]` 引用，默认 `range` 模式通常为空；
- `anchor_positions_json`：被压缩范围内需要重新回放的真实用户消息位置；自动压缩把当前
  活动 Run 的 user 输入（包括 steering）作为候选并只记录已覆盖的交集；空闲手动压缩把最新
  user 作为候选（仍在 raw tail 时不重复记录），rebuild 原样继承活动版本的锚点；未传候选的
  底层调用回退到范围内最新 user；
- `status`：`building → ready → superseded`，失败进入 `failed`。

每个会话最多有一个 `ready` 和一个 `building` 记录，由 SQLite 部分唯一索引保证。发布时还
会再次检查 `parent_id` 是否仍是当前活动版本，避免并发压缩把旧结果覆盖到新结果之上。

## 压缩与恢复流程

### 默认双路径 `a_fallback`

压力触发时，Agent 先选择保留近期原文的安全边界；`StrategyCompactor` 再按完整恢复投影、
摘要生成额度预留及默认 40K 低水位调整边界。前缀路径复用已经发送的主请求快照，只在末尾
追加摘要指令与覆盖范围；原 system、工具、历史、模型、thinking 和生成额度保持不变。
一次可恢复失败后，用独立摘要角色总结同一范围的证据；没有可复用快照时直接走独立路径。
空闲 `/compact` 属于后一种情况。

两条路径共用第三版内容取舍规则：围绕当前任务重新筛选旧摘要与新增原文，在历史之后再次
提醒按八章节输出短要点，目标约 3K tokens。提醒只进入摘要请求，不写入会话原文。
独立请求保持历史 JSON，默认生成额度 8,192，模型继承当前主请求；thinking 默认关闭，
可通过 `compaction_isolated_thinking="inherit"` 恢复继承。历史 JSON 中的 reasoning 不因此删除。
双路径按模型窗口预留校验输入，不进入下述 CURRENT 的固定 60K 分块及候选凝练循环。
候选通过格式、来源、恢复投影预算和净释放检查后才发布；详见
[默认双路径与落地验证](../designs/compaction-dual-path-default.md)。

失败冷却默认 300 秒，保存在当前 Run 的策略实例内；跨 Run、进程重启或新的空闲压缩调用
不会继承它。这与旧 CURRENT 按持久失败范围退避不同，尚未实现跨 Run 冷却。

### 原文尾部与发布投影

`recent_conversation_tokens=20000` 现在是连续 tail 的有界目标：选择器从尾部按完整原子组回扫，
下一个组放不下就停止。为保证至少保留一个最新进展单元，只有“最新单个原子组本身已超过
20K”时允许越界；该组既可能是普通 user/Assistant 消息，也可能是不可拆的 Assistant Tool Call
及其 Tool Result。`compaction_min_recent_user_turns=3` 只用于强制压缩：从尾部回扫，收集到
3 条 user 即停；否则在下一个更旧组会使 tail 超过 20K 时停止，不再为了凑够三轮突破预算。
若一个活动 Tool 轮次超过预算，允许 raw tail 从完整 Assistant 消息开始；被覆盖的真实用户
消息按原始 `role=user` 独立回放，早期执行过程进入派生摘要。默认双路径还会根据完整投影
预算调整边界，因此 20K 不是每次压缩后必定保留的固定额度。

投影时不再把“用户锚点 + 摘要”拼成 synthetic user。压缩范围内的锚点从不可变 Transcript
读取并保持原始 `role=user`；派生摘要使用 `role=assistant, name=context_compaction`，排在锚点
之后、cursor 后的原始 Assistant/Tool tail 之前。这样只有原始 Transcript 用户消息能够支撑
“用户说过”的归因，同时仍保持“用户任务 → 早期执行摘要 → 最近执行原文”的继续顺序。超大
锚点会通过现有 blob 机制外置正文，只在请求中保留有界预览和可恢复引用。

### 旧 CURRENT 的分块与恢复请求

以下输入降级、定期重建、重试/修复/凝练流程描述旧 `current` 策略，不是默认双路径的流程。
旧策略将较早前缀交给 `ContextCompactor`，按 `context.compaction_max_input_tokens` 二次分块，
确保 System Prompt、旧摘要、新增原文和 JSON 包装的总输入不超过压缩预算。两层切分都不会
拆开 Assistant Tool Call 和对应 Tool Result。普通压力每个 Agent step 只压缩一个分块；
`/compact` 可在空闲会话中循环追赶多个分块。

摘要输入对每条消息正文先做 12,000 字符 head/tail 限制，Tool 参数在正常路径保持结构化原值；
若最早的完整原子组仍放不进 48K 规划目标，再依次尝试 2,000 和 512 字符的降级视图。摘要输入
保留 role、content、name、Tool Call/Result 字段，但不携带 `reasoning_content`；来源 SHA-256
仍覆盖原始持久消息的完整字段。若 512 字符降级视图仍放不下，返回
`source_group_exceeds_budget`，不调用模型也不推进 cursor。

首次压缩从原文生成摘要；后续通常用“上一份摘要 + 新增原文”做增量更新。每累计
`context.compaction_rebuild_every` 个成功版本，且完整原文仍能安全放入压缩模型上下文时，
系统从原文重建一次，降低多轮摘要漂移。也可执行：

```text
/compact
/compact rebuild
/compact rollback <compaction-id>
```

其中普通 `/compact` 按配置分流，默认用双路径；`/compact rebuild` 仍直接调用
`ContextCompactor.rebuild()`，重建已有覆盖范围、继承其锚点，不使用第三版双路径提示。

摘要候选先做本地规范化；空响应可按同一生成请求重试一次，格式仍不合法时只携带候选摘要
发起一次修复，长度截断则进入候选凝练。transport 错误同范围重试，只有 Context overflow 才
缩小 Tool 原子范围；鉴权、支付、配置和持久化错误立即停止。每个压缩 Provider 请求有 90 秒
墙钟；一次 `compact()` 默认最多消耗 8 个请求，并以 `$0.25` 为请求间费用停止阈值，这两项门禁
也用于自动压力压缩。费用在一次请求完成后才可得，因此最终一次请求可能使累计值略微越过
阈值。显式 `/compact` 还以 600 秒总墙钟循环多个分块，并按剩余请求数和累计费用停止。普通
Agent Run 另外受 `agent.max_cost_usd` 约束；费用门禁要求配置模型的输入、输出单价。普通压力下
相同 parent/delta 失败后默认退避 300 秒；显式压缩、强制 Provider 恢复和 rebuild 不走这条
退避。

### 各策略共用的恢复与回读

恢复会话时只加载 `ready` 版本。若哈希或摘要结构校验失败，该版本转为 `failed`，系统沿
`parent_id` 自动恢复最近的有效父版本。自动压缩把活动 Run 的用户目标与 steering 作为锚点
候选；手动压缩使用最新真实用户候选，二者都排除带 Skill 交付来源的合成 user，并只记录
实际进入覆盖范围的消息。仍在 raw tail 的 user 不会重复回放；rebuild 保持当前锚点集合。
锚点从 SQLite 原文恢复为独立 `role=user`；若
正文超过通用消息阈值，请求视图会改用有界 head/tail 和可回读 `context_ref`，而 SQLite 事实
源不变。

模型还可按需调用：

- `search_session_history`：在当前会话完整 Transcript 中检索；
- `load_compaction_source`：按压缩 ID 和消息范围读取原文。

## 摘要过大时

系统不会叠加多份摘要。每次发布的新摘要替换旧活动摘要，旧版本和原始 Transcript 保留。
`context.compaction_summary_tokens` 已停用，仅兼容读取旧配置；不会因为完整正文超过旧 4K
门限而拒绝。默认双路径提示以约 3K 为软目标，最终仍要满足恢复后的总输入预算与净释放要求。

`finish_reason=length/max_tokens` 仍视为生成不完整，不能直接发布。默认双路径的前缀失败
最多转独立兜底一次，独立兜底仍失败则保持旧摘要和 cursor，没有新增二次缩短请求。
上文候选凝练仅属于旧 CURRENT。第三版通过主动淘汰无关历史、合并成果和尾部交接提醒
减少超长输出；实验结果及仍会截断的样本见
[内容取舍实测](../evaluations/compaction-selection-20260917.md)。

压缩失败只保证“不发布、不推进 cursor”，不保证当前 Run 一定停止。随后 Context Planner 会
先卸载非 pinned 的 Memory、历史消息组等可选项，可能形成非连续会话视图。history 模式的
本 Run 必需 Skill 交付及其整个工具组必须保留；不能靠删除其中正文使请求勉强通过。
若必留内容和已选 Tool schema 超过硬窗口，存在 Skill 依赖时最多再用一次强制压缩修复机会，
与本 step 的 Provider 超限恢复共用。仍放不下则报告 `context_limit` 或
`skill_context_budget_exceeded`，使用确定性收尾，不继续调用执行模型。

可调整的是后续请求的任务范围、正文/摘要预算或模型窗口；不是让仍绑定的 Skill 静默降为预览。
新摘要已经合法发布但正文恢复失败时，摘要与 cursor 可以保持推进，Run 仍会停止。
这是“摘要发布失败不推进”之外的独立失败阶段。恢复细节及实测开销见
[Skill 历史交付实施与验证](../evaluations/skill-context-validation.md)。

## 配置

```toml
[context]
compaction_strategy = "a_fallback"
recent_conversation_tokens = 20000
# 旧 CURRENT 使用该可配置软目标；双路径提示直接约定约 3K
compaction_summary_target_tokens = 3000
compaction_max_output_tokens = 8192
compaction_failure_backoff_seconds = 300
compaction_request_timeout_seconds = 90
compaction_command_max_requests = 8
compaction_command_max_seconds = 600
compaction_command_max_cost_usd = 0.25
# 强制压缩收集到三条 user 即停，也不加入会使 tail 超过 recent_conversation_tokens 的更旧组
compaction_min_recent_user_turns = 3
compaction_source_refs = "range"
# 只控制 a_fallback 的独立摘要；前缀始终保留主请求 thinking
compaction_isolated_thinking = "disabled"
# 旧策略覆盖项，不影响 a_fallback 两条摘要路径
compaction_thinking = "auto"

# 以下为旧 CURRENT 的生成/恢复参数，不改变默认双路径的两次尝试流程
# compaction_model 留空时，旧策略冻结启动时的 model.name
# compaction_model = "low-cost-summary-model"
compaction_max_input_tokens = 60000
compaction_input_target_ratio = 0.8
compaction_repair_attempts = 1
compaction_condense_attempts = 1
compaction_empty_retries = 1
compaction_transport_retries = 1
compaction_transport_retry_backoff_seconds = 1
compaction_range_attempts = 2
compaction_max_message_chars = 12000
compaction_rebuild_every = 5
```

默认双路径的前缀继承捕获的主请求 thinking；独立路径使用 `compaction_isolated_thinking`，
默认 `disabled`，可选 `inherit`。继承值为未设置时，沿用供应商默认行为。旧
`compaction_thinking` 不覆盖这两条路径。旧策略的 `auto` 同样跟随主模型，旧策略显式
`provider_default/enabled/disabled` 仍可用于历史对照。生成额度是否包含 reasoning 由供应商决定。
`/compact rebuild` 仍走旧重建入口，不使用新的独立兜底开关。

## 测试

`tests/unit/test_compaction_strategies.py` 与 `tests/integration/test_agent_loop.py` 覆盖默认双路径
的前缀保持、独立兜底 thinking 策略、第三版提示接入、空正文与截断拒绝、发布与主任务续跑。

`tests/unit/test_context_compaction.py` 验证事务发布、原文保留、有界分块、分类恢复、候选凝练、
请求超时、范围缩小、失败退避、回滚、模型隔离、范围来源和损坏自动降级。

`tests/integration/test_context_compaction_benchmark.py` 以 10 阶段长任务验证运行时只注入一个
摘要、保留近期原文、Tool 原子性、活动 Run 多 user/steering 锚点、Assistant-safe 切分、原始
消息摘要不变和 snapshot-free。

`scripts/run_compaction_effectiveness.py` 使用确定性或显式开启的真实 Provider 回放 success、
length、format、429、Context overflow 和 authentication 场景，产出请求级 JSONL 与质量门禁。

`tests/integration/test_long_context_cache_benchmark.py` 使用离线确定性 Provider，让同一个任务
经历多次“增长 → 压缩 → 再增长”，并与不压缩反事实比较缓存折算后的单轮成本。生产窗口规模
的 `tests/soak/test_context_cache_soak.py` 由 `RUN_CONTEXT_CACHE_SOAK=1` 显式开启。完整指标、
产物和控制变量方法见 [长任务上下文缓存评测](../evaluations/context-cache-benchmark.md)。

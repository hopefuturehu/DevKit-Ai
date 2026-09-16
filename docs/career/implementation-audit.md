# 面试材料与当前实现核对

核对日期：2026-09-16。代码基线：`2bb51f4`；最近一次运行时代码变更是 `745ad3c`。
返回[面试资料索引](README.md)。本次只整理文档，不修改运行逻辑；历史实验、岗位与外部面经保留原快照。

## 1. 阅读顺序与资料职责

| 资料 | 本次处理 | 使用方式 |
|---|---|---|
| [上下文面试手册](context-management-interview-guide.md) | 修正默认与旧路径混用、输入范围、tail 与输出保留边界，补充恢复机制 | 技术回答的主要入口 |
| [Agent 面经手册](nowcoder-agent-interview-handbook.md) | 区分通用设计建议与已有实现，修正执行时序、验收和预算表述 | 跨模块概念与设计题 |
| [简历素材库](resume-materials.md) | 标明 C08／C09 的历史路径，更新 H03 状态，第 16 节补充最新能力 | 原 42 条及提交清单保留历史范围，当前表述结合后续补充 |
| [招聘调研的 F01—F12](agent-hiring-mainland-2026-09.md#hiring-followups) | 同步取消清理和响应恢复回答 | 岗位导向练习；本轮不更新招聘事实 |
| [答题工作簿](ai-agent-interview-workbook.md) | 核对题目和既有批改；保留已填写答案及未提交改动 | 个人练习区，不把个人原回答当作实现规格 |
| [岗位清单](agent-practice-targets-2026-09-15.md)与[旧问答](../archive/interview-qa.md) | 前者保留招聘快照；后者已有归档标记 | 不用岗位要求或旧 Prompt 布局推断当前能力 |

准备顺序：先核对下表的实现边界，再练主手册或工作簿；要引用数字时回到对应实验的版本、样本与指标定义。

## 2. 已修正的不一致与遗漏

| 原表述或歧义 | 当前代码支持的回答 | 主要证据 |
|---|---|---|
| 压缩故障表把 repair／condense／缩范围和 8 次请求写成通用默认 | 这些属于 CURRENT；默认 `a_fallback` 最多一次前缀＋一次独立摘要，每请求默认 90 秒、两路径总期限 180 秒。空闲 `/compact` 直接独立摘要 | [策略 `summarize_with_fallback`](../../src/bot/compaction/strategies.py)、[Runner `compact_session`](../../src/bot/core/agent.py) |
| 独立兜底“整理同一覆盖范围原始证据”，容易理解为全量重读 | 输入为旧摘要＋从旧 cursor 到新边界的新增原文；发布范围相同，不等于摘要已覆盖的原文全部重新进入请求 | [策略 `summarize_with_fallback`](../../src/bot/compaction/strategies.py)、[父版本与源回读测试](../../tests/unit/test_compaction_strategies.py) |
| 20K tail 被解释为最终一定保留的原文 | 20K 是基础回扫目标；双路径还检查完整恢复投影和摘要输出预留，必要时扩大压缩范围以满足低水位 | [策略 `select_boundary`](../../src/bot/compaction/strategies.py) |
| 候选“长度校验”及 C09 正文上限容易被当作现行 4K 门限 | `compaction_summary_tokens` 仅兼容旧配置，不再限制发布；完整性与完整请求预算仍校验。双路径约 3K 来自提示词，旧 CURRENT 的软目标配置不直接改写该提示词 | [配置](../../src/bot/config/models.py)、[共享提示](../../src/bot/compaction/handoff.py)、[长摘要回归](../../tests/unit/test_compaction_strategies.py) |
| 外置 blob 可以找回任意完整输出 | 只能找回实际采集并保留的正文；命令输出默认最多保留 1,000,000 bytes，上游已截断部分无法凭引用恢复 | [本地执行器](../../src/bot/execution/local.py)、[输出传输](../../src/bot/execution/process_transport.py) |
| Planner “按信任排序”或通用“精确计数” | `trust` 参与来源／角色检查；装箱先保护 PINNED 原子组，再按优先级和位置选择。输入计数依赖模型适配，不能对所有 Provider 承诺精确 | [Planner `pack`](../../src/bot/core/context.py)、[输入计数](../../src/bot/providers/token_counting.py) |
| 执行链只在工具完成后持久化，普通运行总有独立 verifier | 可接受的 Assistant 调用先入库，再执行并保存 Tool Result；usage／事件即时记录。独立 verifier 属于具体 eval case，`completed` 不证明业务完成 | [主循环](../../src/bot/core/agent.py)、[评测 Runner](../../src/bot/evals/runner.py) |
| 八层防线里的幂等键、数据库 ACL 被混入本地已有能力 | 本地没有通用外部写操作幂等协议或数据库授权层；call ID、`idempotent` 注解、重复检测不提供 exactly-once | [工具契约](../../src/bot/tools/base.py)、[策略引擎](../../src/bot/policy/engine.py) |
| 简历 H03 仍笼统称输出截断恢复未落地 | 完整性门禁已生效；截断／主动中断整批响应隔离且不执行工具，自动恢复已实现但默认关闭。可选 B 树形策略也已有运行时实现，非默认策略 | [Runner `_recover_response`](../../src/bot/core/agent.py)、[恢复控制](../../src/bot/core/termination/recovery.py)、[可选策略](../../src/bot/compaction/strategies.py) |
| 长进程取消只讲 PGID／发信号，遗漏最新失败边界 | 默认 5 秒有界清理，跟踪 SID／已观察后代；清理失败保留残留、额度与失败事件，并阻止同批后续工具；平台观察有局限 | [进程范围](../../src/bot/execution/process_scope.py)、[P0 验收](../evaluations/task-reliability-p0-results.md) |
| 用一个“重复检测”开关概括工具拦截、正文中断和恢复 | 工具重复与正文重复是两个开关，均默认 observe；响应自动恢复是第三个开关，默认关闭；不能把已有代码写成默认强制模式 | [配置](../../src/bot/config/models.py)、[流检测](../../src/bot/core/termination/stream_guard.py) |
| 新记忆设计或旧 MVP 状态表被当作当前规格 | 当前仍是 `on_demand/eager`＋Router；`catalog/full` 切换尚未实施。MVP 表停在 09-10 的 schema v15，当前 Store 已是 v16 | [MemoryConfig](../../src/bot/config/models.py)、[Router](../../src/bot/memory/routing.py)、[Store](../../src/bot/sessions/store.py)、[待实施设计](../designs/small-memory-switch-design.md) |

## 3. 面试中需要分开的默认值

以下来自配置类默认值；实际部署可覆盖，不能据此声称用户当前进程一定采用这些参数。

| 配置或行为 | 当前默认 | 容易混淆的边界 |
|---|---|---|
| `context.compaction_strategy` | `a_fallback` | 双路径与单活动摘要分别描述生成策略与持久化方式 |
| `context.compaction_low_water_tokens` | 40,000 | 约束恢复后的完整投影，不是摘要正文长度 |
| `context.compaction_max_output_tokens` | 8,192 | 独立摘要额度；前缀路径继承主请求额度 |
| `skills.context_mode` | `history` | 旧会话可持久保留 legacy 布局；Run 结束不删除历史正文 |
| `memory.context_mode` | `on_demand` | Router 及必需检索门禁仍存在，尚不支持 catalog/full |
| `agent.progress.repeat_guard_mode` | `observe` | 执行前拦截需 enforce；原有进展／停滞控制仍生效 |
| `agent.stream_guard.mode` | `observe` | enforce 只在适用普通正文上中断；reasoning 仅观测 |
| `agent.recovery.enabled` | `false` | 完整性门禁仍生效；开启恢复不代表允许执行截断调用 |
| 恢复额度 | 每 episode 1 次、每任务 2 次；阶段 120 秒 | 跨 resume／压缩保留；首个恢复响应 cap 最多 8,192 且不提高更小的原 cap |
| `agent.process_hard_timeout_seconds` | `None` | 短同步等待不是进程寿命上限 |
| `agent.process_cleanup` | 总计 5 秒；TERM／KILL／排空为 2／2／1 秒 | 清理截止不等于确认全部后代消失，失败必须保留状态 |
| `agent.max_steps/max_wall_time_seconds/max_cost_usd` | `None` | 正常任务没有固定全局预算；显式设置后生效，进展终止另行运行 |

## 4. 验证与结论范围

核对以当前源码及已有回归为依据。以下为本次选取的离线复验入口，覆盖默认值、装箱、双路径、
Skill 历史、响应门禁和进程清理，不调用真实模型：

```bash
.venv/bin/pytest -o addopts='' -q \
  tests/unit/test_config.py \
  tests/unit/test_context_management.py \
  tests/unit/test_compaction_strategies.py \
  tests/unit/test_skill_runtime.py \
  tests/integration/test_skill_context.py \
  tests/unit/test_recovery.py \
  tests/unit/test_stream_guard.py \
  tests/unit/test_process_cleanup.py \
  tests/integration/test_response_recovery.py
```

本轮结果：**137 passed in 6.96s**。面试目录与旧问答共 9 个文件中的 **476 个本地链接及其锚点检查通过**；
`git diff --check` 通过。已填写工作簿、Active Skill 方案和公开基准结果三份原有未提交文件经 SHA-256
核对保持不变，未并入本轮提交。

本轮不重新测量历史缓存、压缩质量、公开任务或 ARM 工具效果，也不更新招聘状态。
Fast 的 35.02%、固定摘要回放 20/20、双路径同题 3/3 等继续沿用各自实验口径；
本轮机制回归不能替换成新的任务成功率、成本收益或平台验收结论。

# 移除 Active Skill 独立全文层：最小实施方案

> 状态：首版 A–C 已实现并通过确定性回归；D 的小规模真实模型验证及范围见[实施与验证记录](skill-context-validation.md)。日期：2026-09-10。
>
> 基于当前工作区核对，承接 [Skill 上下文管理设计](skill-run-lifecycle-design.md) 与 [开源框架调研](skill-unloading-cache-comparison.md)。本文确定首版实施范围；原方案中的独立微裁剪、批量历史投影和通用去重留待后续评测。

目标是保留 `activate_skill`、Skill Catalog 和当前 Run 的激活状态，把正文从历史之前的独立层移到加载位置。后续请求回放同一份历史正文；压缩覆盖正文后，由 Runtime 恢复必要原文，再发出执行请求。

实际落地使用 `skills/runtime.py::RunSkillState` 与 SQLite schema v15 的 `skill_deliveries` 侧表。
新会话默认 `skills.context_mode="history"`；数据库迁移保留已有会话的 `legacy` 布局，首次 Run
选定的模式持久化，fork 继承。`_active_skill_items()` 和原参数链已移除，仅兼容路径保留
`_legacy_skill_items()`。下文保留实施决策与验收目标；通用历史去重、微裁剪和大样本成本评测未实现。

首版完成标准：新模式下不再产生 `ContextLayer.ACTIVE_SKILL` 条目；自动、显式加载及压缩恢复均可交付完整正文；模型每次继续执行前，当前 Run 依赖的正文版本完整可见，或请求明确失败。这个保证不等于模型一定遵守所有规则。

## 1. 改动边界

| 部分 | 首版处理 |
|---|---|
| Skill Catalog | 保留名称、说明和加载入口 |
| `activate_skill` | 保留名称和参数；负责选择、绑定与首次正文交付 |
| `load_skill_resource` | 保留；执行时检查本 Run 绑定及现有目录访问约束 |
| 独立 Active Skill header/body | 从请求构建中移除；有必要时用短尾部状态说明当前有效 Skill |
| 激活状态 | 从共享 `SkillManager.active` 收敛为 Run 所有的状态 |
| 正文 | 自动加载放 Tool Result；显式加载和恢复放带来源的合成消息 |
| 压缩与恢复 | 复用现有摘要、原文 blob 和历史游标，增加交付来源与执行前检查 |
| 任务结束 | 关闭绑定；不因 Run 结束逐条改写历史正文 |
| 失效正文回收 | 先随现有 Planner 预算选择和自然 compaction 处理 |
| 首版以外 | 通用去重、独立微裁剪、按阶段猜测正文依赖、通用历史投影版本系统 |

“移除独立层”不意味着 Skill 下一轮就退出上下文。运行中必要正文继续随历史进入请求，也继续计入 token 和费用。首版主要消除两条重复交付路径，并改变动态加载的位置。

## 2. 目标请求形状

省略与本改动无关的固定层，工具定义独立于 messages：

```text
首次请求：固定基础内容 / Catalog / 记忆 / 摘要 → 已有历史 → 用户任务

自动加载：上述历史 → assistant: activate_skill(A)
                  → tool: A 的完整正文
                  → 后续工具调用和结果 → 短运行状态

显式加载：已有历史 → 真实用户任务 → 合成消息: A 的完整正文
                  → 后续工具调用和结果 → 短运行状态

压缩之后：固定基础内容 → 用户锚点 / 新摘要 → 保留的近期历史
                    → 必要时追加一次 A 的恢复正文 → 短运行状态
```

自动加载必须保留真实的 `tool_call_id` 配对；显式加载和 Runtime 恢复不伪造模型未发出的工具调用。合成消息使用现有不可授权信封，不作为新的 system 或真实用户输入。

同一版本已有完整且可用的历史交付时复用它，不额外注入前置正文。摘要覆盖了旧位置时，在新历史尾部恢复一次，随后继续回放这个新位置。

## 3. 必须先修复的现有路径

| 位置 | 当前行为 | 必需改动 |
|---|---|---|
| [agent.py](../src/bot/core/agent.py) `_activate_skill()` 与工具结果持久化 | 返回正文后仍经过 `_inline_reference()`，可能成为头尾预览 | 为内部 Skill 正文增加完整交付分支 |
| 同文件显式 Skill 处理 | 在真实用户消息落库前激活；正文仅靠独立层重建 | 在真实用户消息后追加显式正文交付 |
| `_externalize_message()` / `_aggressively_externalize()` | 长消息可再次被缩短 | 识别有来源的 Skill 正文；必要正文不可静默降为预览 |
| `_build_context_items()` 及多处用户筛选 | 部分逻辑只检查 `Role.USER` | 区分真实用户和合成 Skill 消息 |
| [context.py](../src/bot/core/context.py) `ContextPlanner.pack()` | 先分 PINNED/optional，再对 optional 分原子组 | 先建立完整工具原子组，再决定整组是否必留 |
| 主循环与 `_termination_context_items()` | 都调用 `_active_skill_items()` | 统一改用有来源的历史和最终可见性检查 |
| [service.py](../src/bot/compaction/service.py) | 历史正文可被逐消息截断或摘要覆盖 | 摘要负责进度；Runtime 按交付记录恢复正文 |

提高正文的 `priority` 不能代替这些改动。仅将 Tool Result 标为 PINNED，也可能把其 Assistant Call 或同批其他结果留在 optional 组，造成协议损坏。

## 4. 最小数据模型与持久化

### 4.1 Run 内绑定

增加 Run 所有的 `RunSkillState`，显式传入执行、资源加载和收尾路径。共享 Catalog 负责发现，状态对象记录本 Run 的选择：

```text
RunSkillState
  session_id, run_id, status(open/finalizing/closed)
  catalog_snapshot
  bindings[name] = {
    version_hash, body_ref, required_range,
    explicit, latest_delivery_position
  }
```

字段是拟议内部接口。`version_hash` 对规范化并按项目既有规则脱敏后的正文计算；blob 与实际交付正文必须使用同一表示，消息信封单独校验。Run 固定目录/正文快照，reload 只影响后续 Run，不从新文件冒充旧版本恢复。

第一版 `required_range` 为完整正文，沿用 `active_skill_tokens=16000` 的聚合上限，同时服从完整请求预算。计数复用当前请求预算接口；16K 不是整份请求的可发送保证。

重复激活幂等。显式指定已有自动绑定时升级来源，修正自动激活名额。新 Run 不从历史“已激活”字样恢复旧绑定；相同版本仍可见时，可以建立新绑定后复用旧交付位置。

### 4.2 一张交付来源表

首版选择在 `messages` 中直接保存有界的完整 Skill 正文，同时保留不可变 blob 供压缩后恢复。增加 `skill_deliveries` 侧表，不把内部字段塞入 Provider 消息协议：

```text
session_id, run_id, message_position
kind = auto_body | explicit_body | restored_body | resource
skill_name, version_hash, body_ref, delivered_range
message_hash, delivery_key
```

约束与读取规则：

- 消息和来源记录在同一 SQLite 事务提交；失败不能留下成功绑定。blob 可以预先保存，孤立 blob 交现有存储维护。
- 来源由内部处理器生成，不接受普通工具正文自报身份。所有历史读取入口关联侧表，包括按位置读取、压缩源读取、恢复与 fork。
- 扩展 `PositionedMessage` 携带内部来源。统一的真实用户判断应覆盖最新用户、用户信任、压缩锚点、近期用户轮数、memory routing 和历史判断；旧的未知记录维持兼容处理，不靠正文猜测来源。
- fork 复制来源关系并按既有 session 规则继承合法 blob 访问权，不继承 active 绑定。
- `delivery_key` 用于同一次加载/恢复重试去重；恢复键包含 Run、正文版本和当前压缩/恢复边界，在同一修复周期中保持稳定。
- 已登记的完整正文在未被压缩覆盖的历史中保持相同表示，不随 active 变化重新截成预览。预算不足时由统一选择或压缩处理，不逐 step 改写正文。

这是对上一版通用投影设计的首版收缩：不新增 `context_view_revisions`，不把旧正文批量改成收据，不修改原始消息。代价是数据库同时保存正文消息和 blob，旧正文在进入压缩前仍占历史空间；首版用现有正文上限约束单次交付。

### 4.3 资源交付

`load_skill_resource` 的访问范围与单次大小限制保持明确；按实际交付给模型的范围登记，不把 blob 中的全文当成完整交付证据。存在截断时明确标记未交付范围。新加载的资源范围保护到取得下一次有效执行模型响应，Provider 拒绝或发送失败不消耗这次保护；随后沿用普通历史管理。

跨多步确实依赖某个资源时，通过明确依赖登记延长保护；首版不自动把整个 Skill 资源目录提升为必留内容。所有资源都计入全局预算。

## 5. 加载、打包和恢复算法

### 5.1 加载

1. 校验当前 Run、Skill 资格和数量限制，从冻结快照取出正文。
2. 检查活动正文聚合预算；已知超限时返回预算错误，不提交新绑定。显式指定失败应在首请求前明确报告，不能假装加载成功。
3. 查找同版正文在当前未覆盖历史中的有效交付。已有完整交付则复用；自动重复调用返回短收据，保留真实 Tool Call/Result。
4. 没有可复用交付时，自动路径生成完整 Tool Result，显式路径生成真实用户消息之后的合成正文；完成来源和消息持久化后提交绑定。
5. 在下一执行请求准备中检查整体预算和实际正文。工具“激活成功”表示绑定成功，不提前声称模型已完整看见。

同一 Assistant 一次请求多个工具时，所有结果都落库并闭合原子组后，才准备下一执行请求。部分 Skill 激活失败仍要保留对应失败 Tool Result，不拆散同批调用。

### 5.2 执行请求准备

首请求、正常下一步、Provider 超限重试、正常最终回答及异常 finalizer 共用一个准备入口：

```text
读取当前历史 + 本 Run 依赖
→ 对已经被压缩覆盖的必需正文，从指定 blob 恢复一次到历史尾部
→ 构建候选，按完整工具原子组预留必要正文和本次资源交付
→ Planner 打包 + Provider 消息适配 + 统一输入预算计数
→ 核对实际发送内容的来源、版本、完整范围和工具配对
    满足：允许发出执行请求
    不满足：使用本 step 的一次修复机会
            → 必要时强制压缩 → 恢复缺失正文 → 重新打包并检查
            → 仍失败：明确终止
```

Planner 必须在分配 mandatory/optional 之前完成原子组划分；组内一项为本次必需依赖时，整个合法组一起处理。必需组连同基础输入放不下时触发修复，不把多余字段或另一条 Tool Result 静默删除。

如果包含 Skill 的旧原子组过大，可以在修复中让 compaction 覆盖它，再追加独立的合成恢复正文，避免为了保留手册而永久保留同组所有旧结果。原组未被覆盖时，优先复用、完整保护；不能每次打包失败都追加一份相同正文。

最终检查不能只查看 `ContextItem.metadata` 或磁盘文件存在性。它必须检查对应正文在最终发送的文本中完整保留，hash/范围符合依赖，未被工具预览、再次外置或 Provider 适配缩短。内部来源字段不发送给模型；适配过程应保留可供本地核对的映射。

### 5.3 压缩与失败边界

摘要允许概括任务进度和引用，不负责逐字保留 Skill 手册。Runtime 持有当前绑定，原文 blob 和交付记录提供恢复依据；历史中仅出现 Skill 名称不算完成恢复。

沿用现有摘要验证和游标发布逻辑。摘要成功而恢复失败时，可以保留已验证的新摘要，但禁止后续任务动作；失败报告给出已有进展和缺失原因。恢复消息及来源记录原子追加。若摘要发布与恢复之间崩溃，重启仍能读取合法摘要与原始引用；新 Run 不静默继承中断的 active 状态。

每个 Agent step 共享一次修复额度，覆盖本地检查失败和 Provider overflow，不能由不同异常入口重新计数。同一边界/版本/预算下失败后不无限重读。引用不可用、版本校验失败或正文仍超预算时返回具体错误；finalizer 无工具时也由 Runtime 恢复，失败则进入有界报告路径。

已实现错误码：`skill_context_budget_exceeded`、`skill_body_unavailable`、`skill_body_version_mismatch`、`skill_body_not_visible`；另有激活数量、不可用 Skill 和关闭作用域错误。自动激活失败返回工具错误，显式加载及已绑定依赖恢复失败则终止 Run。

## 6. Run 结束和缓存策略

Run 状态覆盖工具等待、steering、审批和最终收尾，在最外层 `finally` 关闭；完成、异常、取消和启动失败使用同一清理契约。关闭后才释放同 session 的执行准入并发布 idle，防止自动续跑早于清理。父子 Run 各自关闭，CLI/Web 按 session/run 查询状态。

Run 关闭只终止绑定，不删除历史加载消息。下一 Run 重新选择 Skill；历史正文仍可能影响模型，因此短状态与固定使用规则需要说明适用边界，并通过无关任务测试检查干扰。

在 Skill 功能和目录配置允许时固定控制工具定义及顺序，不因 active 数量增删 `load_skill_resource`。执行端校验是否已激活；渐进工具选择也应保留这些已启用控制工具。实际配置、权限或目录变更仍可改变 schema。

首版只承诺减少 Skill 独立前置层造成的扰动。Planner 丢弃旧历史、发布摘要、恢复正文、环境或其他工具变化，仍可能改变共同前缀。失效正文先由既有预算选择和 compaction 回收；是否值得增加独立裁剪，要用实际任务成本决定。

## 7. 分阶段交付

| 阶段 | 内容 | 完成门槛 |
|---|---|---|
| A：来源与状态 | RunSkillState、交付侧表及事务、真实用户识别、fork/reload/清理兼容 | 合成消息不会成为用户锚点；状态不跨 Run/session 泄漏 |
| B：新交付与恢复 | 完整加载、原子组保护、最终检查、压缩后恢复、有界失败 | 长正文中尾部规则始终可见；所有请求入口通过同一门禁 |
| C：切换组装 | 新模式停用 `_active_skill_items()`，去掉参数链和前置条目；稳定控制工具 | 自动和显式路径均无独立全文副本，工具协议合法 |
| D：行为与成本评测 | 相同样本成对比较旧布局与新布局，包含压缩及异常 | 正确性通过，实际费用与行为证据足以决定启用 |

阶段 B 的确定性检查通过后，新会话默认 `skills.context_mode=history`；旧布局保留作兼容与对照。
默认切换依据是正文完整性与恢复门禁，未把小样本 usage 当作普遍节费证据。

正式切换后移除主循环、压缩后重建、finalizer 中的 `_active_skill_items()` 调用和实现，以及 `_build_context_items(active_skill_items=...)` 参数。`ContextLayer.ACTIVE_SKILL` 不再被新模式使用；旧报告的字符串读取兼容可保留，枚举与相关排序/校验项在无运行依赖后清理。历史报表不必改写。

在 `sessions` 增加持久布局模式，旧会话迁移默认 legacy，新会话按配置创建；history 会话不能在重启后无提示地切回旧前置层。回滚使用已包含来源识别的兼容版本，新会话可恢复 legacy；已写入新格式的会话继续由兼容读取器处理，必要时暂停该会话。不能承诺直接运行完全不识别合成历史来源的旧二进制。

## 8. 验证矩阵

先做确定性检查，再做真实模型行为和计费对照。下表保留完整验收目标；已经执行的范围及结果见[实施与验证记录](skill-context-validation.md)。

| 场景 | 必须观察到的结果 |
|---|---|
| 自动、显式加载 | 正文在正确历史位置；自动 Tool Call/Result 配对；显式内容不成为最新真实用户 |
| 正文超过 4K token 预览、超过 12K 字符但在聚合预算内 | 中部与尾部规则完整交付，不退化为头尾预览 |
| 同 Run 重复激活 | 复用完整交付，模型请求无额外全文副本 |
| 多 Skill、同一次 Assistant 多工具调用 | 完整原子组保留或整体进入修复，不出现孤立调用/结果 |
| 必需组接近预算与 Provider 重打包 | 按完整请求计数；最终正文不被静默删短 |
| 执行中压缩一次、连续多次压缩 | 被覆盖的活动正文按原版恢复；同一恢复边界幂等，无无限恢复循环 |
| blob 丢失、版本变化、恢复仍超限 | 明确终止；不继续任务动作，不把失败描述为完成 |
| finalizer 无工具、取消与持久化异常 | 使用同一检查；关闭状态，释放准入，不影响其他 Run |
| restart、fork、reload、自动续跑 | 来源/引用合法；新 Run 独立绑定，旧版正文不会被新文件替换 |
| 无关后续任务、连续相关任务 | 记录旧正文干扰与重新激活成本，不能只断言 active 已清空 |
| 压缩后首个行为依赖手册尾部规则 | 验收任务产物或工具参数符合规则，不能只测试摘要包含 Skill 名称 |

对应扩展 [Skill 单测](../tests/unit/test_skills.py)、[会话存储单测](../tests/unit/test_session_store.py)、[Planner 单测](../tests/unit/test_context_management.py)、[压缩单测](../tests/unit/test_context_compaction.py)、[Agent 集成测试](../tests/integration/test_agent_loop.py) 与 [handoff 集成测试](../tests/integration/test_context_handoff.py)。

成本对照至少覆盖短任务、长工具循环、多 Skill 切换、压缩后继续、Skill 后接无关任务。固定模型、正文、预算与任务样本，隔离缓存预热，分别报告：

- 正文重复 token、首次差异位置、工具 schema 变化和恢复次数。
- Provider 实际 cached/uncached token、总费用、压缩与回读开销、延迟；字段缺失时标记不可观测。
- 任务通过率、规则执行正确性、恢复失败、旧 Skill 干扰和超限次数。

已有 [历史裁剪分析](history-pruning-analysis.md) 没有可验证 Skill 激活样本，不能拿其中约 8% 的容量潜力证明本方案收益。发布前应补齐上述样本，不预设节省比例。

## 9. 改动文件地图

| 文件 | 实施职责 |
|---|---|
| [skills/models.py](../src/bot/skills/models.py)、[skills/catalog.py](../src/bot/skills/catalog.py) | 快照、Run 绑定、版本、幂等和资源资格检查 |
| [sessions/store.py](../src/bot/sessions/store.py) | 交付来源侧表、原子追加、布局模式、历史/fork 查询 |
| [core/context.py](../src/bot/core/context.py) | PositionedMessage 来源、真实用户识别、完整组预算、角色校验与排序 |
| [core/agent.py](../src/bot/core/agent.py) | 显式/自动加载、受控外置、统一准备门禁、恢复、Run 清理及旧层移除 |
| [compaction/service.py](../src/bot/compaction/service.py) | 合成来源排除用户锚点，压缩输入与恢复协调，保留既有验证契约 |
| [providers/base.py](../src/bot/providers/base.py)、[providers/openai_compatible.py](../src/bot/providers/openai_compatible.py) | 核对最终适配内容和统一预算接口，不建立第二套计数规则 |
| [cli/runtime.py](../src/bot/cli/runtime.py)、[cli/app.py](../src/bot/cli/app.py)、[web/server.py](../src/bot/web/server.py) | Run 状态归属、按 session 查询、reload/new、并发与续跑准入 |
| [config/models.py](../src/bot/config/models.py) | 临时布局开关，沿用现有预算；定型后清理过渡配置 |

落地顺序是 A → B → C → D；其中 B 的正文完整性和恢复保证是切换前置条件。首版以这条闭环完成独立全文层移除，再根据实测决定是否扩展正文回收策略。

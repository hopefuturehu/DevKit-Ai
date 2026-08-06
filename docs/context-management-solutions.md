# 上下文管理优化方案（历史分析）

> **⚠️ 本文档为历史分析记录，保留以供设计溯源。**
> 文中分析的是旧版 `ContextSnapshot` + `compact_messages()` 系统的问题。
> 当前代码已演进为 **可恢复的单摘要压缩** 方案（详见
> [recoverable-context-compaction.md](recoverable-context-compaction.md)），
> 旧版 `ContextSnapshot` 和 `compact_messages()` 已不再用于生产路径，
> 相关表结构仅保留向后兼容读取。
>
> 方案中提出的 LLM 摘要（第3节）、消息指针回溯（第4节）、多级触发（第6节）、
> 防重复摘要（第10节）等优化方向已在单摘要版本链中以不同形态实现。
>
> 状态：历史存档
> 日期：2025-07（分析阶段），2026-07（标注存档）
> 前置文档：[design.md](design.md)、[implementation-status.md](implementation-status.md)

---

## 1. 概述

当前上下文管理系统在分层信任模型、原子组保护、快照不堆叠、双层预算校验和内容外置等方面设计成熟。但在以下方面存在可优化空间，本文档逐项分析并给出方案。

问题按影响面分为三类：

| 类别 | 问题 | 影响 |
|------|------|------|
| 精度与质量 | Token 估算偏差、快照提取启发式、语义分组弱 | 裁剪决策不准、压缩后信息丢失 |
| 策略与时机 | 裁剪粗暴、压缩触发单一、REHYDRATABLE 无自动刷新 | 上下文利用率低、过期信息残留 |
| 工程健壮性 | 快照不可逆、缺少构建缓存、兼容函数重复摘要风险 | 长会话性能退化、边界场景异常 |

---

## 2. Token 估算精度提升

### 2.1 问题

`TokenEstimator` 采用 `ASCII/4 + CJK/1` 的启发式规则。对于混合中英文、代码块、JSON 结构、数字序列等内容，与实际 BPE tokenizer 的偏差可能达到 ±30% 以上。虽然有二轮 `exact_token_repack` 校准，但如果估算偏差过大，第一轮裁剪就已做出错误取舍——例如误判某段代码"很便宜"而保留了实际 token 数很高的 JSON，丢弃了更关键但看起来"贵"的中文指令。

### 2.2 方案：模型感知的混合估算

**核心思路**：在保留当前无网络依赖的快速估算作为冷启动兜底的同时，逐步引入更精确的计数源。

**阶段一（低成本）**：Provider 返回精确 usage 后，异步回写校准因子。

- 每次模型调用返回 `input_tokens` 后，计算 `校准因子 = 实际 tokens / 估算 tokens`
- 按消息类型（system/assistant/user/tool）和内容特征（纯 ASCII、含 CJK、含 JSON、含代码）分别维护滑动窗口平均校准因子
- 后续估算时：`校准后估算 = 启发式估算 × 对应类型的校准因子`
- 冷启动时校准因子为 1.0，随使用逐渐收敛

**阶段二（中成本）**：引入可选 tokenizer 库。

- 检测 `tiktoken` 是否可用；如果模型是 OpenAI-compatible 系列，加载对应编码（如 `cl100k_base`）
- 优先使用精确计数，降级到校准后的启发式估算，最后降级到原始启发式
- tokenizer 库作为可选依赖，不强制安装

**阶段三（高成本，按需评估）**：Provider 侧 token 计数 API。

- 部分 Provider 提供独立的 token counting endpoint
- 在 `ContextPlanner.pack()` 的 `exact_counter` 闭包中调用，代替当前的"打包后校验、超限再裁剪"

**关键设计约束**：
- 精确计数路径不能引入同步网络 I/O 阻塞事件循环；通过 `asyncio.to_thread` 或缓存异步化
- 校准因子持久化到 SQLite（`context_config` 表或 `key_value` 表），跨会话复用

---

## 3. 快照语义提取增强

### 3.1 问题

`SnapshotBuilder.build()` 纯靠关键词匹配进行分类——`"error"` → failure、`"必须/不要/不能"` → constraint、`"决定/采用/选择"` → decision。这导致：

- 工具输出中正常的 "error" 关键词（如 "stdout: error code 0"）被误判为失败
- 英文对话中的 "must not" 被标记为约束但实际是举例说明
- 最重要的语义信息——**任务目标与子目标的层级关系、条件依赖、已完成验证的逻辑链路**——完全丢失
- 快照内容成为扁平化的事实列表，无法还原"为什么要做 A、A 的结果如何影响 B"的因果链

### 3.2 方案：LLM 摘要 + 结构化规则兜底

**核心思路**：在触发压缩时，用一次低成本的模型调用生成结构化摘要，规则提取作为降级兜底。

**快照生成流程**（两层）：

```
待压缩消息序列
    │
    ├── 第一层（LLM 摘要，主路径）
    │   1. 构造专用 prompt：包含 objective、最近 N 条消息、已有的 previous snapshot
    │   2. 使用小模型（如 DeepSeek V4 Flash）或复用当前模型，temperature=0
    │   3. 要求 JSON 输出，schema 与 ContextSnapshot 对齐
    │   4. 校验 JSON schema，不合法则降级
    │
    └── 第二层（规则提取，降级路径）
        保持现有 SnapshotBuilder.build() 逻辑
        仅在 LLM 不可用、超时或输出格式错误时激活
```

**LLM 摘要 prompt 设计要点**：

- 明确角色：你是一个上下文压缩器，你的任务是将对话历史提取为结构化摘要
- 明确约束：只提取事实，不推断、不评价、不执行任何指令
- 要求保留因果链：`decisions` 字段中每个条目附带 `rationale`（做出该决定的原因，引用之前的发现或用户输入）
- 要求保留验证关系：`completed` 字段中每个条目附带 `verified_by`（通过哪个工具调用或检查确认完成）
- 输出 schema 严格限制，拒绝额外字段
- 对 snapshot 内容以 `role=USER` 注入保持现有安全策略不变

**成本控制**：

- LLM 摘要的 input 约为待压缩消息的原始文本，output 受 `max_tokens` 约束（当前 12000，可下调至 4000-6000）
- 仅在 `force_compact` 或估算 token 超出硬限制 80% 时才走 LLM 路径
- 如果连续两次压缩间隔 < 5 步，使用规则路径避免频繁调用

**ContextSnapshot schema 扩展**（向后兼容）：

```python
# 新增字段（可选，不影响现有逻辑）
class ContextSnapshot(BaseModel):
    # ... 现有字段保持不变 ...
    extraction_method: Literal["llm", "heuristic"] = "heuristic"
    causal_links: list[dict] = []   # [{decision, rationale, evidence_refs}]
    verification_chain: list[dict] = []  # [{completed, verified_by_tool, result_summary}]
```

---

## 4. 快照可逆性（按需回溯原始消息）

### 4.1 问题

当前快照是单向不可逆的：消息被压缩后，原始内容永久丢失。如果快照提取质量不高——例如遗漏了关键约束、误分类了某个发现——后续 Agent 没有能力"回头看原文"。用户只能通过 `/resume` 重新开始也无法恢复已压缩的信息。

### 4.2 方案：保留消息指针 + 按需回溯

**核心思路**：快照中的 `source_message_positions` 已记录了原始消息的位置。在此基础上增加一个 `load_context_reference` 类似的"回溯原始消息"能力。

**设计要点**：

- 快照中 `source_message_positions` 从当前的"仅记录位置"扩展为记录 `(position, content_hash, token_estimate)` 三元组
- 不保留原始消息全文（避免快照膨胀），但保留定位信息
- 新增 `load_archived_message` 内部方法（非 Tool）：根据 `position` 和 `session_id` 从 SQLite messages 表中按需读取原始消息
- 在 `_build_context_items` 中，当快照的某个字段（如 `constraints` 或 `failures`）被模型引用且需要更多上下文时，Agent 可通过 `load_context_reference` tool 回溯
- 更直接的方式：在快照 `model_message()` 内容的每个条目后附加 `[ref:pos=N]` 标记，模型在需要时能明确引用

**安全约束**：

- 回溯只允许读取**已被快照覆盖的消息**，不能通过回溯绕过压缩机制读取裁剪掉的内容
- 回溯后的原始消息以 `role=USER` 注入（与快照一致），防止权限提升
- 回溯读取的消息计入当前上下文预算

**替代方案（更轻量）**：

如果不引入回溯机制，可以在快照中增加 `confidence` 字段标记提取质量。当 LLM 摘要路径产生快照时 confidence=high；规则路径 confidence=low。confidence=low 时，压缩更保守（保留更多最近消息，快照预算下调）。

---

## 5. 裁剪策略从 FIFO 升级为加权优先级

### 5.1 问题

`SnapshotBuilder._fit_to_budget()` 按固定列表顺序 `list_fields` 从头删除元素：

```python
list_fields = (
    "source_message_positions",  # 最先被删
    "findings",
    "completed",
    ...
    "constraints",               # 最后被删
)
```

这导致：
- `source_message_positions` 总是第一个被牺牲，但它恰恰是唯一能回溯原始消息的线索
- 每个字段内部从 `[0]`（最早的元素）开始删除，但最早的元素可能比最新的更有价值（例如最开始的 `objective` 相关的 findings）
- 缺乏对元素信息密度的判断——一个 50 字的精确错误信息和一个 200 字的泛泛描述被同等对待

### 5.2 方案：信息密度加权的优先级裁剪

**核心思路**：给每个列表元素赋予信息密度分数，优先丢弃低密度元素。

**信息密度评分维度**：

| 维度 | 高密度信号 | 低密度信号 |
|------|-----------|-----------|
| 长度效率 | 短文本包含具体信息（路径、数字、错误码） | 长文本但多为连接词、模板话术 |
| 唯一性 | 包含 sha256 hash、UUID、文件路径 | 重复出现或高度相似的文本 |
| 时效性 | 最近 3 轮内的内容 | 超过 10 轮前的内容 |
| 可操作性 | 包含具体参数、配置值 | 模糊的描述或已解决的临时状态 |

**裁剪算法**：

```
1. 为每个列表的每个条目计算 density_score ∈ [0, 1]
2. 将所有条目按 density_score 升序排列
3. 从最低分开始删除，直到 snapshot token_estimate <= max_tokens
4. 同分的条目：FIFO（旧先删）
5. 保留 source_message_positions 不低于原始长度的 20%（保护回溯能力）
```

**密度评分的简化实现**（不引入额外模型调用）：

```python
def _density_score(text: str, age_rank: int, total_count: int) -> float:
    # 长度效率：短且包含结构化信息得分高
    length_score = min(1.0, 200 / max(1, len(text)))
    # 结构化信号：包含数字、路径、错误码
    struct_score = 0.5 if any(c.isdigit() for c in text) else 0
    struct_score += 0.3 if "/" in text else 0
    # 时效性：越新越重要
    recency = 1.0 - (age_rank / max(1, total_count))
    return 0.3 * length_score + 0.3 * struct_score + 0.4 * recency
```

**替代方案（更低成本）**：

如果不想增加复杂度，至少做以下两个调整：

1. 从 `list_fields` 中移除 `source_message_positions`，改为只保留最近 100 个，超出部分不存入快照
2. 将 `findings` 和 `completed` 的裁剪方向从"删最早"改为"交替删除"（一次删最早、一次删最晚）

---

## 6. 压缩触发时机从单一阈值升级为多级渐进

### 6.1 问题

当前仅在 `_run_loop` 每次迭代开始时检查 `unplanned_tokens > target_input_limit`，触发一次性的 `_checkpoint_conversation`。如果一个 tool call 返回了大量数据（例如 `read_file` 返回 20000 token 的文件），会在下一次循环开始时突然发现严重超限，导致一次性裁剪过多消息。

### 6.2 方案：多级压缩阈值 + Tool 返回后即时评估

**三级压缩阈值**：

| 级别 | 触发条件 | 动作 |
|------|---------|------|
| ⚠️ 预警 | 估算 > target × 0.9 | 发出 `CONTEXT_WARNING` 事件，不做压缩 |
| 🔶 软压缩 | 估算 > target | 执行 `_checkpoint_conversation`（现有行为），但只压缩 `force=False` 能覆盖的最旧消息 |
| 🔴 硬压缩 | 精确 > hard_limit | 强制压缩 + 裁剪 active_skill/memory/rehydratable 层 |

**Tool 返回后即时评估**：

在 `_run_loop` 中，每次 tool 执行完成、结果消息追加到 conversation 后：

```
1. 计算当前 conversation 中最后一条 tool 消息的 token 增量
2. 如果增量 > target_input_limit × 0.3（单条消息消耗超过 30% 预算）：
   → 立即对该消息执行 externalize（内容外置），不等下次循环
3. 如果 conversation 估算 > target_input_limit × 0.95：
   → 在追加 tool 结果后立即触发 soft compaction
   → 而非等到下一轮循环开始时
```

**Active Skill 的渐进降级**：

当触发硬压缩时，不直接丢弃 skill 内容，而是：

1. 将完整 SKILL.md → 替换为 catalog 中的 skill summary（一段话描述）
2. 如果仍超限 → 替换为 skill name + "需要时使用 activate_skill 加载"
3. 最终手段 → 从上下文移除，但记录到 runtime_note 中告知模型

---

## 7. REHYDRATABLE 层的自动刷新机制

### 7.1 问题

`SKILL_CATALOG`、`MEMORY` 和 `ACTIVE_SKILL` 标记为 `REHYDRATABLE`，语义上表示"可从数据源重新加载"。但实际上除了用户手动 `/skills reload` 外，没有自动刷新机制。如果 Skill 目录在运行中变化（用户新增/修改了 Skill 文件），或者长期记忆被另一个会话修改，当前运行的 Agent 不会感知。

### 7.2 方案：变更检测 + 主动刷新 + 事件通知

**Skill 目录变更检测**：

```
1. ContextAssembler 在构造时记录 Skill 目录的 mtime 和文件列表哈希
2. 在每次 _build_context_items 时（每次循环迭代）做低成本检查：
   - os.stat(skill_dir).st_mtime 是否变化
   - 如果变化，重新扫描目录，计算新的 catalog
   - 比较新旧 catalog 的 skill 名称集合和 description
3. 如果检测到变更：
   - 发出 CONTEXT_STALE 事件
   - 自动重建 skill_catalog 相关 ContextItem
   - 对于新激活的 skill（之前不在 catalog 中的），不自动激活（安全考虑）
   - 对于已删除的 skill，从 active_skills 中移除并发出通知
```

**代价控制**：

- 只在上下文构建时检查（每个 step 一次），不在 tool 执行期间检查
- `os.stat` 是纯系统调用，延迟 < 1ms
- 如果目录下有数千文件，改用 `os.stat` + 文件数量对比（不逐个计算哈希），仅在数量变化时才全量扫描

**长期记忆刷新**：

- 当前实现已改为 Markdown 记忆：`USER.md` 是显式记忆，`MEMORY.md` 是自动主题文件的
  生成索引，两者只在 Run 开始时装配一次。
- 运行中的记忆视图保持稳定；`/remember` 是空闲会话命令，自动提取也只影响后续 Run，
  避免同一次模型循环的上下文无提示变化。
- 需要核验细节时调用 `search_memory` 和 `load_memory_evidence`，不在每个 step 轮询文件。

---

## 8. Conversation Grouping 从单层 Tool 关联升级为语义任务边界

### 8.1 问题

`_conversation_groups()` 仅按 `tool_call_id` 将 assistant(tool_calls) 和 tool(result) 分为一组。对于多步推理中语义相关但跨 tool 调用轮次的对话——例如：

```
User: 分析 A 的性能瓶颈
Assistant: [调用 ksys collect]
Tool: {ksys 结果}
Assistant: 基于 ksys 结果，瓶颈在函数 X，用 tuner 深入分析 [调用 tuner hotspot]
Tool: {tuner 结果}
Assistant: 根据 tuner，X 的问题是 Y，建议修改 Z
```

这个完整的"性能分析"任务跨越了 3 个 tool 调用轮次，但在当前分组逻辑中会被拆成 3 个独立组。裁剪时可能保留 tuner 结果但丢弃 ksys 结果，导致模型失去因果链的起点。

### 8.2 方案：可选的语义任务边界检测

**核心思路**：在保留现有 tool_call_id 原子组的基础上，增加一层"任务边界"标记，将跨轮次但同属一个子任务的 message 组标记为"建议一起裁剪或一起保留"。

**任务边界检测方法**（按复杂度递增）：

**方法一（低成本）：用户消息作为边界**。

- 最简单的启发式：两个 user message 之间的所有 assistant/tool 消息属于同一个任务
- 裁剪时：如果某个 tool 组要被丢弃，丢弃从上一个 user message 开始到该 tool 组之间的所有内容（整个任务）

**方法二（中成本）：模型标记任务边界**。

- 在 system prompt 中增加一条指令：当开始执行一个新的子任务时，在 assistant 消息中以 `[task: 描述]` 开头
- `_conversation_groups` 检测 `[task: ...]` 模式，将其后的消息归入新的语义组
- 不强制模型使用，未检测到时降级为方法一

**方法三（高成本，远期）**：LLM 后验标注。

- 在压缩时，用 LLM 摘要调用同时输出消息分组建议
- 复用第 3 节的 LLM 摘要调用，一个请求同时完成摘要和分组

**推荐路径**：先实现方法一（成本极低，代码改动约 20 行），同时预留方法二的 prompt 指令位置。

**数据结构扩展**：

```python
@dataclass
class ContextItem:
    # ... 现有字段 ...
    semantic_group: str | None = None  # 方法二/三填充
    # atomic_group 保持不变
```

`ContextPlanner.pack()` 在裁剪时增加一条规则：同一 `semantic_group` 的消息要么全部保留，要么全部丢弃（或全部 checkpoint）。

---

## 9. 上下文构建缓存

### 9.1 问题

`AgentRunner._build_context_items()` 在每次循环迭代时重新构建整个 `ContextItem` 列表，包括重新创建 base_items、memory_items、active_skill_items、snapshot item、遍历 conversation 等。对于长会话（50+ 轮），这个 O(n) 遍历加上每个 item 的 token 估算有可观的 CPU 开销。

### 9.2 方案：增量构建 + 版本化缓存

**核心思路**：上下文构建结果缓存，仅在输入变化时重建。

**缓存键设计**：

```python
cache_key = (
    hash(base_items),           # 固定在运行开始时
    hash(memory_items),         # 变化频率极低
    hash(active_skill_items),   # skill 激活/停用时变化
    snapshot.cursor_position if snapshot else 0,  # 压缩后变化
    len(conversation),          # 每步 +1~2
    tuple(r.id for r in runtime_notes),  # 运行时偶尔变化
)
```

**增量更新**（关键优化）：

当 `len(conversation)` 仅增加 1-2 条消息时（绝大多数步骤），不重建整个列表，而是：

```
1. 从缓存中取出上一次的 context_items
2. 移除上一次的 conversation 部分（标记为 layer=RECENT_CONVERSATION/TOOL_RESULT 的 item）
3. 追加新的 conversation 消息对应的 ContextItem
4. 更新缓存
```

**缓存生命周期**：

- 缓存绑定到 `(session_id, run_id)`
- 在 `_checkpoint_conversation` 后（snapshot 变化）全量重建一次
- 在 skill activate/deactivate 后全量重建一次
- 运行结束后清除

**收益评估**：

- 对于 30 步的运行，假设前 5 步触发压缩、后 25 步稳定执行：缓存命中率 ≈ 80%
- 每次避免的遍历和 token 估算开销 ≈ O(conversation_length)，在长会话中效果显著

---

## 10. compact_messages 防重复摘要

### 10.1 问题

`compact_messages()` 是兼容旧接口的独立函数。它每次调用独立创建 `SnapshotBuilder` 并生成快照，如果调用方在已经压缩过的 messages 列表上再次调用（例如外层循环），会产生"快照中嵌套引用快照"的退化。虽然当前代码路径中 `_run_loop` 不使用此函数，但作为公开 API 缺乏防护。

### 10.2 方案：快照幂等标记

**方案**：

- `ContextSnapshot.model_message()` 生成的消息 content 中已包含 `[被压缩的旧会话摘要 / Context checkpoint: ...]` 标记
- `compact_messages()` 在扫描待压缩消息时，检测是否已存在此类标记
- 如果 `conversation` 中连续出现 ≥ 2 个 snapshot 消息 → 表明已被压缩过 → 合并而非嵌套
- 合并策略：保留最新的 snapshot（包含最多的 cursor_position），丢弃旧的 snapshot 消息
- 同时发出警告日志

**更根本的方案**：废弃 `compact_messages()`，将所有压缩路径统一到 `AgentRunner._checkpoint_conversation()` 中，确保只有一条压缩代码路径。

---

## 11. 优先级与实施路径

按投入产出比和风险排序：

| 优先级 | 项目 | 收益 | 成本 | 风险 | 建议时机 |
|--------|------|------|------|------|---------|
| P0 | 裁剪策略优化（第5节） | 高：直接提升压缩质量 | 低：约 30 行 | 低 | 下一个迭代 |
| P0 | 防重复摘要（第10节） | 中：消除边界 bug | 低：约 15 行 | 极低 | 下一个迭代 |
| P1 | 多级压缩触发（第6节） | 高：避免突发超限 | 中：约 60 行 | 低 | 下下个迭代 |
| P1 | 语义任务边界（第8节） | 中：保护因果链 | 低-中：方法一约 20 行 | 低 | 下下个迭代 |
| P2 | Token 估算精度（第2节） | 中：渐进改善 | 中：校准因子约 80 行 | 低 | 按需 |
| P2 | 自动刷新机制（第7节） | 中：改善动态场景 | 中：约 70 行 | 低 | 按需 |
| P2 | 上下文构建缓存（第9节） | 中：性能优化 | 中：约 100 行 | 中（缓存一致性） | 长会话场景验证后 |
| P3 | 快照语义提取增强（第3节） | 高：大幅提升压缩质量 | 高：LLM 调用成本 + 约 200 行 | 中（LLM 稳定性） | 评测验证当前方案不足后 |
| P3 | 快照可逆性（第4节） | 中：安全网 | 中：约 80 行 | 低 | 与第3节联动 |

**路径建议**：

```
Phase 1（1-2 个迭代）：P0 项 → 改善现有压缩质量，消除已知缺陷
Phase 2（2-3 个迭代）：P1 项 → 改善压缩时机和结构化
Phase 3（评测驱动）：   P2/P3 项 → 通过评测数据验证是否引入 LLM 摘要
```

P3 项（LLM 摘要）虽然收益最高，但引入 LLM 调用作为压缩的依赖项会带来成本、延迟和稳定性风险。建议先通过 Phase 1-2 的改进提升现有规则路径的质量，然后通过评测对比（规则压缩 vs LLM 压缩的错误率和任务完成率）来量化 LLM 摘要的实际增益，再决定是否引入。

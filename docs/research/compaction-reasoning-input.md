# 历史 reasoning 是否进入压缩输入：判断、对照与验证

> 2026-09-17 边界补充：下文“当前 bot”指 9 月 9 日的 CURRENT 快照。
> 现默认 `a_fallback` 的独立请求会把证据视图中的 `reasoning_content` 序列化进历史 JSON；
> 前缀路径沿用主请求序列化规则。白名单过滤只属于旧 CURRENT，历史规模估算不代表现默认。
> 当前路径见[上下文组装](../architecture/context-assembly.md#大消息和-reasoning-如何计入)。

核对日期：2026-09-09。本文讨论主 Agent 的历史 reasoning 是否作为摘要模型的输入材料。
摘要模型自身是否开启 thinking、它生成的 reasoning 是否保存或回放，见
[压缩 reasoning 的保存与回放](compaction-reasoning-replay.md)，不能混用这三个指标。
本轮仅做源码研究和离线审计，没有新增模型调用或修改运行器策略。

60K 输入预算的由来、超限后的分块与重试，以及其他框架处理超大历史的具体边界，见
[超大上下文的摘要输入预算与恢复](oversized-context-compaction.md)。超过单次预算不等于
整段历史无法压缩；应让新增 reasoning 参与分块计数，并验证实际覆盖范围。

## 1. 当前 bot：原生字段不传，正文内的思考文字仍可能传

当前调用链是：

```text
原始消息（content / reasoning_content / tool_calls 等）
  → _render_message：白名单选择字段，省略 reasoning_content
  → _summary_request：历史 JSON 放进一个 user.content
  → Provider._payload：专用 system + user；不带 tools
```

`_render_message()` 保留 position、role、name、tool_call_id、tool_calls、content，
没有读取 `reasoning_content`。历史角色只作为 JSON 数据，历史 assistant 不会逐条变成
摘要 API 请求中的 assistant。正常单条 content 上限为 12,000 字符，工具参数另有降级规则。
增量压缩使用 previous_summary + new_messages，原始重建使用 raw_messages；两者共享该过滤。
[渲染与请求代码](../../src/bot/compaction/service.py)、
[最终发送序列化](../../src/bot/providers/openai_compatible.py)。

**这不是“全部思考文字都被清除”。** 当前代码没有剥离 content 中的 `<think>` 或
`<reasoning>` 块。如果 Provider 已把思考混入正文，它仍可能随有界 content 进入摘要输入；
之前摘要里已经存在的相关内容也可能作为 previous_summary 继续传入。

离线标记探针使用同一条合成消息，在原生 reasoning_content 放一个独有标记，在 content
内的 `<think>` 放另一个。经过实际 renderer、request builder 和 Provider `_payload()` 后：

| 检查项 | 结果 |
|---|---|
| 原生 reasoning 独有标记在最终 HTTP body 中 | 否 |
| content 内 think 独有标记在最终 HTTP body 中 | 是 |
| API 消息角色 | system、user |
| tools 字段 | 无 |

这验证的是本地发送边界，不是远端模型注意力或摘要质量。原始记录包含 reasoning，或完整
记录的 source_sha256 包含该字段，都不能证明模型见过它。日志 `reasoning_chars` 统计的是
**压缩输出**，不能用其为 0 推断**历史输入**不含 reasoning。

## 2. 开源实现没有统一选择

以下都是固定提交中的指定路径；“纳入”仅指被选中历史里已保存的可读 reasoning。

| 项目 | 是否纳入历史 reasoning | 实际实现 |
|---|---|---|
| 当前 bot | 原生字段不纳入 | 有界历史 JSON 只保留白名单字段；未剥离正文内 think 块 |
| Pi | 纳入 | `thinking` block 变成 `[Assistant thinking]` 文本，再放进摘要 user prompt |
| OpenCode `packages/opencode` | 纳入 | reasoning part 变成 `[Assistant reasoning]` 文本，再组织摘要 prompt |
| Hermes 普通批量摘要 | 排除 | 只读取可见 content；同时主动剥离助手正文的内嵌 think/reasoning 块 |
| DeepSeek Harness | 有条件纳入 | 原 messages、system、tools 交给 adapter，再追加摘要指令；自带 DeepSeek serializer 仅为带 tool call 且 reasoning 非空的助手写原生 reasoning_content |

[Pi 序列化](https://github.com/badlogic/pi-mono/blob/a4453b79bb8d66b5f385b28ae0e33843c947504a/packages/coding-agent/src/core/compaction/utils.ts#L109)、
[Pi 摘要调用](https://github.com/badlogic/pi-mono/blob/a4453b79bb8d66b5f385b28ae0e33843c947504a/packages/coding-agent/src/core/compaction/compaction.ts#L623)、
[OpenCode 序列化](https://github.com/anomalyco/opencode/blob/da4730e4a41dcbb2cb2d907dd2b06ac481b8f962/packages/opencode/src/session/compaction.ts#L54)、
[Hermes 过滤](https://github.com/NousResearch/hermes-agent/blob/eac1e25127a75c61116a0bbb8ce7681a2f8b61b4/agent/context_compressor.py#L3532)、
[Harness 请求](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/compaction/compaction-basic/src/summarizer.ts#L121)、
[Harness serializer](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/llm/llm-deepseek/src/serialize.ts#L70)。

Hermes 的代码注释给出的理由是节省摘要上下文，并避免把临时推演中的结论固化为事实；这是
设计理由，不是这些任务上的对照实验结果。Pi/OpenCode 的实现则证明历史 reasoning 可以
作为摘要资料使用；仅凭源码无法证明哪一种任务质量更高。

还有一层容易漏掉：DeepSeek 当前官方文档规定，不带 tools 时，原生 reasoning_content
即使回传也会被忽略；带 tools 的 thinking 请求应回传历史 reasoning 并将其拼入上下文。
因此“wire JSON 里存在字段”也不总等于“模型消费了文本”。Pi/OpenCode 放在普通 prompt
正文中的文本与原生协议字段不同，不受上述原生字段忽略规则影响。
[官方思考模式协议](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)。

当前 bot 的摘要请求无 tools。若要让它读取历史 reasoning，应明确加入历史数据 JSON；
仅开启摘要 thinking，或在外层消息补一个原生字段，都不是等价实现。主会话工具链必须按
目标 Provider 协议回放，不能把摘要材料的裁剪规则套用到活动工具链上。

## 3. 两次真实压缩：全量加入的输入规模

取停止快照中真正被压缩的区间，按当前默认 renderer 重建，再仅给非空历史 reasoning
添加 `historical_reasoning` 数据字段。区间、正文、工具参数、摘要 prompt 均固定。
基线 source_chars 和 planned_input_tokens 均与原事件逐项一致；DB/WAL 哈希与快照一致，
读取前后不变。结果来自补充运行 Chess attempt 4、SymPy attempt 2；主样本 18 个任务未触发
压缩，不能把下面两次当作 18 个任务的压缩对照。

| 指标 | Chess | SymPy |
|---|---:|---:|
| 被压缩消息位置 | 1–93 | 1–143 |
| 区间内 reasoning 非空消息数 | 39 | 58 |
| 省略的原生 reasoning 字符数 | 152,193 | 154,883 |
| 当前摘要输入估算 tokens | 37,786 | 36,722 |
| 同区间全量加入后的输入估算 tokens | 76,753 | 76,531 |
| 估算增量 | 38,967（103.13%） | 39,809（108.41%） |
| 原摘要调用实际 API input tokens | 37,674 | 47,876 |

两组全量输入估算均超过当前 60,000 压缩输入上限及 48,000 规划目标。若让新增字段参与规划，
压缩器需要缩短单次覆盖范围、分块或采用其他预算方案；不能保持其他条件不变直接全量加入。
**这些是固定范围的反事实估算，不是新增 API 调用的实测 token 或节省率。** SymPy 基线实际
用量就高于估算，后续必须校准 Provider 计数，不能把新增估算量直接加到实测用量上当作结果。

两次摘要生成成功，也不证明丢弃 reasoning 没有损害信息。此前后续请求的 reasoning 字段
兼容故障属于主会话回放，不能证明“摘要输入遗漏 reasoning”导致故障，也不能证明全量传入
能修复该故障。[既有量化结论](../evaluations/context-compaction-effectiveness.md)。

## 4. 如何决定是否传入

判断依据应是**新增信息价值、误记风险和完整管线成本**，而不是字段名字。

- 值得保留：尚未落在可见回答、工具结果或其他任务状态中的关键决策原因、待验证假设、已排除
  路径及排除原因、未完成步骤。纳入时要带消息位置、证据关联和状态，假设不能写成已验证事实。
- 可以省略：与可见记录重复的复述、已被后续证据推翻的推演、无助于继续任务的尝试。用户约束、
  工具执行证据和当前文件状态应优先保留；reasoning 不能替代执行证据。
- 需要预算权衡：加入历史思考后是否挤掉原始证据、减少覆盖范围、增加摘要请求次数或触发输出
  截断。只比较单次摘要长度会漏掉这些成本。

建议先保持当前默认，增加可实验的 `none / full / selected` 输入策略，而不直接改为全量。
其中 `none` 要明确仅排除原生字段，还是也识别正文中的思考块；不能用通用标签正则删除用户
提供的代码或文档。`selected` 是待验证方案：有界保留上述任务状态及其来源。若用另一个模型
从全量 reasoning 筛选，需要把筛选调用的输入、延迟、费用和误选一起计入，不能宣称免费降本。
长期也可以让关键决定进入显式任务状态，减少它们只存在于临时思考记录的情况。

## 5. 验证方法：分别回答“有没有传”和“传了有没有用”

1. **发送边界验证**：独有合成标记分别放入原生字段、正文和工具结果，经过完整 renderer 和
   最终 HTTP serializer 检查。区分输入 reasoning 字符数、实际纳入量、排除量、额外估算
   tokens、覆盖范围；按字段记录长度/哈希即可，不必把整段历史重复写入运行日志。
2. **内容可用性验证**：先按 Provider 文档确认字段语义，再用合成事实探针检查模型能否提取
   仅在该输入通道存在的信息。一次未提取可能是摘要选择或随机性，不能独立证明未传入。
3. **质量对照**：固定源区间、任务、摘要模型、thinking、摘要长度预算和后续保留尾部，比较
   none、full、selected。先在三组都能容纳的样本上比较内容价值，再在生产输入上固定总预算，
   比较分块、覆盖范围和总成本；两种实验分别报告，避免范围变化成为混杂因素。
4. **验收内容**：覆盖“仅思考中有下一步”“错误假设随后被工具否定”“与可见记录重复”三类
   样本；检查关键事实/待办召回、假设误记为事实、重复已失败动作、后续工具执行及产物通过率。
   对每组重复运行，报告样本量和不确定性，同时统计全部辅助调用及恢复后的总 tokens、费用、
   延迟、缓存命中、压缩成功/截断率。HTTP 200 和章节齐全不能代替任务质量。

只有 selected 在相同预算下稳定改善继续执行质量，且误记与总成本可接受，才有依据调整默认。
当前两次离线规模审计尚未完成这种质量验证，也未实现该策略。

## 6. 复现与来源

```bash
.venv/bin/python scripts/analyze_compaction_reasoning_input.py \
  > /private/tmp/compaction-reasoning-input.json
```

[离线脚本](../../scripts/analyze_compaction_reasoning_input.py)只读 SQLite，校验快照哈希、区间及
两项基线指标，运行最终 payload 合成标记探针；不请求模型、不输出历史 reasoning 文本。
[固定审计结果](../data/compaction-reasoning-input.json)包含源文件与 DB 哈希。

四个开源项目本轮各下载一份上述固定提交的输入路径源码，均与本地文件 SHA-256 一致；其他
关联文件沿用[上轮固定源码核对](compaction-reasoning-replay.md#6-固定版本及核对范围)。
结论不代表它们最新主分支或全部 Provider 路径。

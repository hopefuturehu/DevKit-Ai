# A32 首次压缩工具调用重放

2026-09-11（北京时间）。重放首次压缩输入三次，**三次均返回工具调用，没有生成摘要**。
其中两次要求重复刚执行过的 `objdump`，一次要求重复写入已有计划。所有调用只记录，
没有执行；本次没有修改运行时压缩或收尾逻辑。

## 具体申请了什么、打算做什么

| 重放 | 工具 | 可观察的意图 | 输出 tokens | 结果 |
|---|---|---|---:|---|
| 1 | `run_shell` | 再次读取二进制 `.rodata` 的同一段常量 | 77 | tool_calls，无摘要 |
| 2 | `update_plan` | 再次写入当前四项计划，状态没有变化 | 129 | tool_calls，无摘要 |
| 3 | `run_shell` | 与第一次完全相同 | 77 | tool_calls，无摘要 |

两次 `run_shell` 的完整参数均为：

```json
{"script":"cd /app && objdump -s -j .rodata --start-address=0x485ac0 --stop-address=0x485ae0 mystery"}
```

这条命令读取 `mystery` 二进制 `.rodata` 中的 32 字节，供逆向分析常量使用。它与首次
压缩前主循环第 30 步的调用参数完全相同（原事件 sequence 5775）。原调用已经完成，
输出也包含在本次输入的第 70 条历史消息中。因此，本轮申请的是重复取证。

`update_plan` 的完整参数为：

```json
{
  "items": [
    {"content": "Analyze mystery binary (symbols, constants, disassembly)", "status": "completed"},
    {"content": "Write mystery.c ray tracer reconstruction", "status": "in_progress"},
    {"content": "Compile and verify output matches image.ppm byte-for-byte", "status": "pending"},
    {"content": "Verify compressed size < 2k", "status": "pending"}
  ]
}
```

这些内容和状态与压缩前最近一次 `plan.updated`（sequence 3116）完全相同，且已有计划
也以运行时提示传给了模型。本轮请求没有推进任何计划项。

## 能说明什么

本次观察表明，模型仍在输出原任务的执行动作或计划维护动作，未成功切换到只写交接摘要。
三次均没有调用 `load_compaction_source`、`load_context_reference` 或历史搜索工具；
已有上下文也包含它想再次读取的结果。**这些样本不支持“放行工具就能补齐摘要信息”**：
放行会重复命令或计划写入，之后是否能生成摘要还没有验证。

输入保留了原 Agent 的工具定义、计划维护要求和弱进展提示，最后才追加摘要指令及
“只输出交接摘要，不调用工具”。这些执行提示可能干扰模式切换，但本次没有改变提示
做对照，不能据此确定是哪一条指令导致。三次响应分别只使用 77、129、77 个输出 tokens，
远低于 8,192 上限，且结束原因为 `tool_calls`，因此本轮失败不属于输出长度截断。

## 重放输入与可信范围

- 来源为 `path-tracing-reverse` 的 A32 正式运行，在首次 `context.compaction.started`
  （sequence 5781）之前截断。只读原 SQLite 数据库，读取前后 SHA256 一致。
- 恢复历史位置 1–70、当时的计划与 `progress-stall-warning`。没有使用任务结束后的计划。
  首次压缩尚无已发布摘要、待恢复 Skill、一次性检索结果或存活的受管进程。
- A 的摘要请求使用整个已组装上下文。虽然本次准备替换的历史范围是 1–40，实际喂给
  摘要模型的历史范围是 1–70；两者不能混为一谈。
- 沿用原摘要提示、17 个工具定义、`tool_choice=auto`、temperature 0、thinking disabled、
  `deepseek-v4-pro` 和摘要输出上限 8,192。A32 的 32K 是主循环输出额度，并非摘要额度。
- 工具模块、上下文、交接提示、进展控制、Skill 目录模块及六个组装辅助函数与冻结 Worker
  wheel 做了源码或 AST 核对，19 项均一致。具体哈希及检查项见结果 JSON。
- 两处 token 检查点均与旧记录一致：首次主请求 **3,914**，首次摘要请求 **73,262**。
  三次重放 API 实际输入也均为 **73,262**，fingerprint 均为
  `a307abda487cd1b463329ccb945ce396`，与原测试相同。
- 原始 HTTP 请求没有持久化；环境根据运行资料恢复为 Linux x86_64、无 ksys/devkit、
  无 AGENTS.md、空 Skill 目录。计数与源码核对提供一致性证据，但不能证明请求逐字相同。
- 三次是相同输入的独立采样，间隔 5 秒，未追加前一次输出、工具结果或纠正指令。
  这验证的是该压缩边界的重复出现情况，不能作为多任务失败率。

**当时两次失败的工具名和参数仍然无法恢复。** 旧记录只保存 `calls=True`，输出长度为
129 和 77；它们恰好与本轮计划调用和 shell 调用长度一致，只能视为同类行为的旁证，
不能据此把新响应归给旧请求。背景见 [此前调查](tool-choice-cache-probe.md)。

## 费用、产物与验证

三次请求 usage 完整，总输入 **219,786**、命中 **219,648**、未命中 **138**、输出 **283**。
加权命中率 **99.94%**。按调用时 Pro 峰时价格计算约 **$0.01097**，这是 usage 计算值，
不是账单读数。预算预设 $0.50，全未命中加输出预留为 $0.40195。
[官方价格](https://api-docs.deepseek.com/quick_start/pricing/)

- [重放脚本](../scripts/replay_compaction_tool_calls.py)：只调用模型并记录完整事件，不派发工具。
- [逐请求结果、完整参数、输入审计和来源哈希](data/compaction-tool-replay-results.json)。
- 本地完整请求、流式事件和 manifest：`artifacts/compaction-tool-replay-20260911/live/`。
- 已核验来源和产物哈希、token 收支、三次调用参数与历史/计划的一致性；脚本通过编译
  和 Ruff 检查。运行时文件没有变化。

脚本依赖本地原测试产物，默认只重建请求；只有显式添加 `--live` 才产生三次 API 调用。
再次调用前需按日期核实模型与价格，使用新的输出目录。

```sh
.venv/bin/python scripts/replay_compaction_tool_calls.py --output artifacts/compaction-tool-replay-new
```

# Terminal-Bench 上下文中等任务集结果

本报告记录 `context-medium-six` 的首次完整运行，目标是测量 Bot 当前上下文管理链路在六个
中等长度工具任务上的任务完成率，并检查真实运行是否触发自动压缩。

## 结论

- 有效的六任务样本通过 `1/6`，任务完成率为 **16.7%**。
- 六个有效 Trial 的自动压缩完成/失败均为 `0/0`，压缩覆盖率为 **0%**；因此“发生压缩的
  Trial 完成率”没有定义，不能用本次 `16.7%` 推断压缩机制提高或降低了完成率。
- 六个 Trial 都有事件流，最大单轮 prompt 为 `89,165` tokens，低于本次配置的自动压缩
  触发线 `96,000` tokens。没有发生上下文溢出或压缩错误。
- `mailman` 在单轮 prompt 最高只有 `64,871` tokens 时累计消耗了 `2,990,184` input
  tokens，并先撞到 `$3` 费用上限。这暴露出当前触发器只处理“单轮上下文压力”，不会因
  “跨多轮累计费用”主动压缩。
- 失败分别来自实现正确性、费用上限、模型输出退化、任务执行超时和模型服务断连；没有一个
  可以归因为“压缩丢失了必要信息”。反过来，由于没有压缩样本，也不能证明压缩保留了信息。

## 实验配置

| 项目 | 值 |
|---|---|
| 日期 | 2026-08-30 |
| 数据集 | `terminal-bench/terminal-bench-2-1` |
| Harbor | `0.20.0` |
| 模型 | `deepseek-v4-flash` |
| Agent 并发/重复 | `2 / 1` |
| 子 Agent | 关闭 |
| Bot 内部上限 | 240 steps、7200 秒、每任务 `$3` |
| 宿主环境 | macOS ARM64；Linux/AMD64 task 镜像通过 Docker 模拟运行 |
| Agent wheel SHA-256 | `e417143635b632c83020f3fc8fa8db3d5a2aacc57ca4b35cbb0a2e4d002e7b10` |
| 主 Job | `artifacts/terminalbench/jobs/2026-08-30__13-35-05` |
| 基础设施重试 Job | `artifacts/terminalbench/jobs/2026-08-30__14-37-46` |

运行命令为：

```bash
.venv/bin/python scripts/run_terminalbench.py \
  --suite context-medium-six \
  --n-concurrent 2 \
  --n-attempts 1
```

这里的 7200 秒只是 Bot 自己的防失控上限。Harbor 仍执行每个 Terminal-Bench task 发布的
官方 agent timeout，所以该配置消除了旧的统一 1800 秒上限，但没有改变任务自身的时间语义。

## 被测上下文管理机制

本次 Bot 配置使用 `131,072` token 模型窗口、`120,000` 最大输入，并为输出、协议和安全
分别预留 `4,096`、`2,048`、`2,048` tokens。硬输入上限因此是 `120,000`，自动压缩阈值为
其 `80%`，即 `96,000` tokens。

每次模型请求前的处理顺序是：

1. 从不可变 Transcript 恢复当前会话，把 System policy、项目说明、环境、按需记忆、Skill、
   最近对话、运行时提示和 Tool schema 建模为带优先级与保留策略的 `ContextItem`。
2. 过大的消息或 Tool Result 只保留首尾摘录，完整内容写入 context blob；模型需要时可通过
   `load_context_reference` 分块恢复。这一步会在压缩之前限制大输出重复回放。
3. 先估算未裁剪请求。如果超过 `96,000` tokens，则保留最近约 `20,000` tokens 且至少三个
   user turn，把更旧的完整对话范围压成一个可追溯摘要。
4. 压缩摘要带来源范围和 SHA-256，以 `context_compaction` 派生上下文注入；原始 Transcript
   不改写，需要核验时可通过 `load_compaction_source` 读取。
5. 最后由 Context Planner 按预算打包，并修复 Tool Call/Tool Result 协议配对后发送给模型。

Terminal-Bench 的每个 Trial 都是独立新会话，没有可复用的既有个人记忆。因此本次主要覆盖
最近对话、Tool Result 外置、Context Planner 和自动压缩触发器，不测量自动记忆检索质量。

## 六任务结果

初次运行 `custom-memory-heap-crash` 时，Docker Hub 拉取返回
`registry-1.docker.io/v2/: EOF`，Agent 根本没有启动。预拉取镜像后单独重跑成功。该次重试
替代基础设施失败进入六任务分母；如果把未执行 Agent 的拉取失败也当成业务样本，会把
Agent 完成率与基础设施可用性混在一起。

| Task | Reward / 状态 | Steps | Tools | 最大 prompt | 压缩完成/失败 | 结果或直接失败点 |
|---|---|---:|---:|---:|---:|---|
| `custom-memory-heap-crash` | `1.0 / completed` | 48 | 62 | 46,312 | 0/0 | 通过 |
| `filter-js-from-html` | `0.0 / completed` | 15 | 16 | 24,952 | 0/0 | 12 个干净 HTML 中有 5 个被错误修改 |
| `mailman` | `0.0 / limit_reached` | 76 | 85 | 64,871 | 0/0 | 达到 `$3`；leave 流程没有生成确认邮件 |
| `path-tracing-reverse` | `0.0 / limit_reached` | 17 | 25 | 48,238 | 0/0 | 模型输出达到长度限制，未创建 `/app/mystery.c` |
| `large-scale-text-editing` | `0.0 / AgentTimeoutError` | 51 | 55 | 57,443 | 0/0 | 任务的 1200 秒 agent timeout；Vim 宏仍未结束 |
| `llm-inference-batching-scheduler` | `0.0 / failed` | 31 | 33 | 89,165 | 0/0 | 模型 API 断连，尚未写出两个 plan 文件 |

五个正常回收 Agent 计费元数据的 Trial 合计为：

- input tokens：`6,209,806`；
- output tokens：`160,591`；
- 已记录费用：`$6.530988`；
- `large-scale-text-editing` 在 Harbor 超时终止，未生成 Agent 计费汇总，以上合计不包含它。

## 失败归因

### `filter-js-from-html`

Agent 正常完成并声称干净 HTML 可以 byte-identical 保留，但 verifier 的
`test_clean_html_unchanged` 显示 12 个样本有 5 个被改写。另一个安全过滤测试通过，说明实现
选择了过度清洗或重建标签，违反“无 JavaScript 内容保持不变”的要求。这是实现正确性失败，
不是运行中断或上下文丢失。

### `mailman`

三个 verifier 测试通过两个；`test_join_announce_leave_flow` 找不到 From 以
`reading-group-confirm` 开头、Subject 含 `leave` 的确认邮件。Trace 显示 Agent 已发现 leave
请求进入 SMTP，但仍在检查服务日志时达到 `$3`。这是未在预算内完成服务配置/排障。

它同时说明当前 `96,000` 单轮压力阈值不能控制长循环的累计成本：76 步的最大 prompt 只有
`64,871`，所以没有压缩，但反复回放仍累计到近 300 万 input tokens。

### `path-tracing-reverse`

Agent 一直停留在 `objdump` 数据与反汇编分析，最后一轮输出重复同一批观察直至模型输出长度
上限。Verifier 的三个测试全部失败，首个原因是 `/app/mystery.c` 根本不存在。该失败发生在
生成任何候选实现之前，属于模型输出退化和执行策略失败。

### `large-scale-text-editing`

Harbor 在 task 发布的 1200 秒上限处报告 `AgentTimeoutError`。事件流仍回收出 51 个模型 step
和 55 个已完成 Tool Call；Agent 启动的 Vim 大文件编辑长时间运行并持续轮询。Verifier 随后
也显示 Vim 宏在自己的 600 秒上限内未完成，且 `/app/expected.csv` 缺失。这是算法/工具执行
时间问题，不是 Bot 的旧 30 分钟内部限制；受超时取消延迟影响，Harbor 记录的 agent execution
墙钟时间为 1621 秒。

### `llm-inference-batching-scheduler`

Agent 已找到 bucket 2 的多组可行参数，准备验证 bucket 1 时，provider 返回
`Server disconnected without sending a response.`。终止前没有创建
`output_data/plan_b1.jsonl` 和 `plan_b2.jsonl`，所以 verifier 六项只通过一项。最大 prompt
`89,165` 已接近但仍低于压缩线；这是 provider 中断，不应记成 verifier-only 的能力失败。

### `custom-memory-heap-crash`

排除首次镜像拉取 EOF 后，重试在 48 步、62 次 Tool Call 内通过 verifier。它是本组唯一成功
样本，也证明六任务 runner、容器内 Agent、事件回收和 verifier 链路可工作。

## 这轮数据能支持什么

本次可以支持以下结论：

- 在固定模型、单次尝试和当前预算下，Bot 整体上下文管理链路的六任务完成率是 `16.7%`；
- 最高 89K 的真实 prompt 没有越过 96K 触发线，六次均无 context limit 和 compaction error；
- 大 Tool 输出外置和规划器可能抑制了窗口增长，但没有关闭外置的对照组，所以这里仅是运行
  现象，不能写成因果收益；
- 单轮 token 压力与累计 token/费用是两个不同目标；当前压缩触发器只覆盖前者。

本次不能支持以下结论：

- 压缩后的任务完成率是多少；
- 压缩是否提高任务完成率或节省总费用；
- 保留或移除 Tool Result 对完成率的因果影响；
- 与 Terminal-Bench leaderboard 的正式可比成绩。当前是 macOS ARM64 上的单次本地运行，
  且包含一次明确标记的基础设施重试。

若要专门验证压缩，需要在相同六任务、模型、预算和 task seed 下增加配对变体，例如保持其他
机制不变，只把压缩阈值降到保证多次触发的水平，并同时报告压缩覆盖率、压缩后继续执行步数、
任务完成率、总输入 tokens 和失败分类。没有实际触发压缩的 Trial 不应进入“压缩后完成率”
分母。

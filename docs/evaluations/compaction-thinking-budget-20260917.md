# 独立兜底摘要 thinking 预算对照（待线上运行）

2026-09-17；生产代码基线 `627ef16`。

**当前状态：离线预检和 85 项相关测试通过；线上请求为 0，尚无本次成功率。**
自动审批拒绝了线上测试启动，要求用户明确授权将下述四份含私有对话历史的输入
重新发送到原有 DeepSeek 官方服务。未使用其他通道重试；生产配置、摘要和游标未修改。

## 固定实验条件

使用 [9 月 16 日事故记录](compaction-incident-20260916.md) 中全部四种不同的
`a_isolated` 请求。五次历史失败中有两次输入完全相同，按 request blob SHA-256 去重，
避免重复输入获得额外权重。历史的 **0/5** 不作为本次对照组结果。

| 组别 | 请求 thinking | `max_tokens` | 含义 |
|---|---|---:|---|
| `current` | 不传，沿用提供商默认值 | 8,192 | 原样重放现方案 |
| `thinking_off` | `disabled` | 8,192 | 关闭兜底思考，额度全部可用于正文 |
| `thinking_reserve_8k` | `enabled` | 16,384 | 8K 正文预算之外，额外预留 8K 生成额度 |

第三组是**预算规划上的分离**。DeepSeek Chat Completions 只有共享的生成上限，
没有独立的 reasoning/text 硬额度；不能保证 thinking 最多 8K，也不能保证正文一定获得 8K。
对应参数和使用量字段见 [DeepSeek 请求接口](https://api-docs.deepseek.com/zh-cn/api/create-chat-completion/)。

每个输入、每组各两次：`4 × 3 × 2 = 24` 次。固定随机种子安排请求顺序，并发 3，
每次仅尝试一次，整体请求超时沿用 bot 的 90 秒。不修改提示词、历史、温度、工具列表、
输入边界或正文软目标。此实验只重放独立兜底，不调用前缀阶段。

| 摘要覆盖终点 | 历史服务端输入 tokens | 本次预算估计 tokens | 16K 生成时的输入上限 |
|---|---:|---:|---:|
| 238 | 77,750 | 81,602 | 110,592 |
| 248 | 83,751 | 87,903 | 110,592 |
| 253 | 88,189 | 92,563 | 110,592 |
| 259 | 102,607 | 107,702 | 110,592 |

原模型名保持 `deepseek-v4-flash`。按[官方模型与价格页](https://api-docs.deepseek.com/quick_start/pricing/)
在本次查阅时的说明，旧 Flash 名称已路由到 V4.1 Flash；记录服务端返回型号，
不能把重放当作固定旧模型版本的实验。全部输入未命中缓存、全部输出达到额度时，
24 次的高峰价格估算上界为 **$0.980159**。价格可能变化，该值不是账单保证。
计价依据：每百万 token 缓存命中 $0.006、未命中 $0.30、输出 $1.20；空闲时段减半。

## 计分与范围

- 主指标 `candidate_pass`：实际调用 `StrategyCompactor.summarize(attempts=1)`，通过现有
  正文非空、非 length、八章节、来源范围、无工具调用、正常结束等校验。保留真实错误分类。
- 额外指标 `body_budget_pass`：主指标通过，且服务端 completion 减 reasoning tokens
  不超过 8,192。防止把第三组的“正文也扩大了”误认为单纯解决思考挤占。
- 报告 thinking、正文、总输出、缓存命中、耗时、失败类型与估算费用；缺失 usage 标记为未知。
- 保存正文便于检查关键事实，但格式通过不代表事实无遗漏。本次不做任务续跑质量评估，
  不执行生产发布及替换检查，不能声称测出了完整压缩流水线或所有会话的成功率。
- 四个输入来自同一个、预先筛选的失败会话，且互相重叠；每组八次只能说明这批困难输入。
  跨试验缓存预热也会影响耗时及费用。

## 复现与记录

脚本：[test_compaction_thinking_budget.py](../../scripts/test_compaction_thinking_budget.py)。
离线预检读取生产 SQLite 时使用 `mode=ro`，校验 request blob 哈希，确认三组 HTTP payload
仅有 `thinking`/`max_tokens` 差异，使用提供商 tokenizer 预检窗口。响应、原文和临时数据库
只写入 Git 忽略的 `artifacts/`；报告不复制历史正文。每次必须使用新的输出目录。

```bash
.venv/bin/python scripts/test_compaction_thinking_budget.py \
  --output artifacts/compaction-thinking-20260917-dry-new

# 仅在获得原始历史发送权限之后执行：
.venv/bin/python scripts/test_compaction_thinking_budget.py \
  --output artifacts/compaction-thinking-20260917-live --live
```

执行后生成 `manifest.json`、固定脚本副本、每次 `result.json` 和总 `results.json`。
在获得线上结果之前，不改变生产兜底策略。

验证：Ruff 通过；`test_compaction_thinking_replay.py`、`test_compaction_strategies.py`、
`test_context_compaction.py` 共 **85 passed**。包含 length 被拒绝、空正文、章节缺失、
流未正常结束、正文独立超预算与缺失 reasoning 使用量等计分边界；这些是离线测试，
不能代替真实 API 成功率。

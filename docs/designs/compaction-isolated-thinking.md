# 独立摘要兜底默认关闭 thinking

日期：2026-09-18。

在 96K / 256K / 512K 的真实历史压缩实验中，独立摘要关闭 thinking 为 9/9 发布成功，
开启为 8/9；唯一失败为正常 `stop`、5010 个 reasoning tokens、正文为空。
本轮未出现 `length`，不能证明此前长推理耗尽输出预算的问题已修复。详细数据、缓存表现与
摘要保真限制见[实验报告](../evaluations/long-context-compaction-20260918.md)。

基于兜底优先保证产出正文的目的，只给生产 `a_fallback` 的独立请求增加独立开关。

```toml
[context]
compaction_strategy = "a_fallback"
compaction_isolated_thinking = "disabled"
```

| 路径 | thinking 行为 |
|---|---|
| 主任务与压缩后续跑 | 沿用主模型配置 |
| `a_prefix` | 保留捕获的主请求设置，包括未设置时的供应商默认值 |
| `a_isolated`，默认 `disabled` | 显式发送关闭 thinking；模型仍取捕获的主请求 |
| `a_isolated`，可选 `inherit` | 继承捕获的主请求 thinking，包括 `None`；恢复此前行为 |
| 空闲 `/compact` | 无前缀快照，直接使用上述独立路径设置 |

旧工作区没有新字段时自动采用 `disabled`；`bot init` 显式生成该默认值。可通过
`bot config set context.compaction_isolated_thinking inherit` 恢复继承。已有 CLI/Web 进程
需要重启以加载源码和配置。旧 `compaction_thinking` 仍可加载，但不覆盖默认双路径；
旧 `current/a/b` 和 `/compact rebuild` 不使用新开关。

只改变独立请求新生成的 reasoning，不移除历史 JSON 中的 reasoning 证据，不修改原始
Transcript、用户锚点、尾部或捕获的主请求。前缀结构与参数保持不变；独立请求已重组 role，
缓存命中需单独计量，不能承诺关闭 thinking 会沿用前缀缓存。

## 失败保护

- 仍最多一次前缀、一次独立请求；没有新增续写或重试循环，独立生成额度仍为 8,192。
- `stop` 但没有正文（包括仅 reasoning）、`length/max_tokens`、流未结束、错误章节或
  工具调用都不能发布；摘要请求中的工具永不执行。
- 关闭 thinking 不代表供应商必然产出可用正文；独立请求仍经过相同校验、来源检查、
  完整恢复预算与净释放检查，失败则保留旧 checkpoint、cursor 和原文，并进入既有退避。
- 鉴权/付费等不可恢复错误、取消、来源变化继续中止，不盲目发起第二次请求。
- 请求审计继续记录实际 thinking、finish reason、usage、正文与 reasoning，便于区分
  正常完成、仅推理、截断以及供应商错误。

## 验证

配置与初始化测试覆盖默认关闭、显式关闭、恢复继承和非法值。策略测试覆盖三种主请求
thinking 与独立策略的组合，核对真实 Provider payload，确认快照不变，审计记录匹配。
失败矩阵包含仅 reasoning、空正文、截断、流中断、工具调用、超时和传输错误；两路失败时
检查不发布、原文不变、退避生效，以及已有 checkpoint 不被覆盖。

Agent 集成测试覆盖自动压缩的前缀失败、独立发布、主任务续跑，以及空闲 `/compact`
的正常发布和截断拒绝。实验脚本显式冻结继承基线，独立开启/关闭对照组继续分别发送
`enabled/disabled`。本次落地验证使用模拟供应商与本地序列化，没有新增付费 API 请求。

上述配置、CLI、策略、实验脚本、旧压缩器、Provider 和 Agent 回归共 **237 项通过**；
Ruff 与 `git diff --check` 通过。首次受限沙箱运行因本地进程查询/管理限制出现进程用例
超时，已中止；同一组测试在允许进程管理的环境中完整运行，14.09 秒全部通过。
当前工作区加载确认：`a_fallback`、独立 `disabled`、主模型 thinking 仍为 `None`。

这些检查验证控制流与发布安全，不证明摘要事实或结束条件的语义完整性；9/9 的样本
成功也不是稳定性保证。历史原文和用户锚点仍是摘要之外的恢复依据。

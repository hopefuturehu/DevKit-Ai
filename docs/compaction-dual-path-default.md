# 默认启用双路径压缩并继承主模型 thinking

日期：2026-09-13。实现基于此前留档提交 `a5728fc`；本次未进行付费模型测试。

## 运行行为

- 默认 `context.compaction_strategy` 从 `current` 改为 `a_fallback`。配置模型、`bot init` 模板和 README 同步更新；未显式配置策略的已有工作区直接采用新默认值。
- 自动压缩先尝试复用已发送主请求的前缀，再加摘要后缀；一次可恢复失败后，使用独立摘要角色重新总结同一范围的原始证据。两条路径都失败时不发布新根，保留旧摘要与原始记录。摘要请求中的工具调用仍不执行。
- 前缀请求保持主请求的模型、thinking、工具及输出额度。独立兜底也读取同一请求快照的模型与 thinking；不受旧 `compaction_thinking` 配置或请求快照之后的配置变化影响。
- `model.thinking="enabled"`、`"disabled"`、未设置三种情况分别传递开启、关闭、供应商默认值。未设置不等于关闭；最终 API 行为仍受供应商协议适配约束。
- `ContextCompactor.thinking_mode` 改为动态读取。双路径始终跟随主模型；旧策略的 `auto` 也改为跟随主模型。旧策略显式 `enabled/disabled/provider_default` 选项仍可用于历史对照，不能覆盖双路径的设置。
- 旧 CURRENT/A/B 策略保留为显式选项，既有 CURRENT 评测配置显式固定策略，避免默认值变化偷偷改变历史基线。

## 空闲会话的 /compact

原来 `AgentRunner.compact_session()` 无论配置什么策略，都直接进入 CURRENT 压缩器。本次为默认双路径接入同一摘要发布与验证流程。

空闲会话没有可复用的已发送请求快照：运行结束后这些快照已清理。因此 `/compact` 记录 `idle_session_snapshot_missing`，直接进行一次独立摘要，不假定缓存命中。构造完整恢复投影时包含系统/项目/环境、记忆、工具定义、计划、旧摘要及近期历史，并沿用外置内容和 Skill 历史处理。

这一入口沿用命令的时间、请求次数和成本预算；独立请求使用当前主模型与 thinking，支持主模型在压缩器创建后切换。原始 Transcript 不被修改，生成截断不发布 checkpoint。

## 本次没有改变的长度规则

- 摘要正文目标通常 3,000，发布门限仍为 **4,000 启发式 tokens**。
- 前缀生成额度继承主请求，**并非固定 32K**；32K 是此前 Flash 评测的主请求配置。
- 独立兜底的生成额度默认 **8,192**。
- `finish_reason=length/max_tokens` 与正常 `stop` 但正文超过 4K 都不能发布。前缀失败最多转独立兜底一次，没有新增二次缩短流程。
- 双路径仍按模型窗口预留计算摘要输入预算，不进入 CURRENT 的固定 60K 区间循环。

开启 thinking 时，生成额度是否包含 reasoning、如何分配，取决于供应商；这次没有增加生成额度，也没有用模拟测试证明开启 thinking 能提高摘要事实准确率。

## 验证

`pytest tests/unit tests/integration --tb=short`：**557 passed, 7 skipped**。另有一个既有 Starlette/httpx 弃用提示，无失败。

新增/调整验证覆盖：

1. 未配置策略和 `bot init` 生成的配置均默认启用双路径。
2. 自动压缩实际进入前缀→独立兜底→续跑流程；三种 thinking 设置前后一致，摘要器申请的工具不执行。
3. 前缀失败后，独立兜底以捕获的主请求为准，不被旧压缩配置或后续主配置修改覆盖。
4. 空闲 `/compact` 使用独立摘要，跟随创建压缩器之后切换的主模型/思考设置；遵守单请求限制，原文摘要哈希不变。
5. 空闲压缩收到 `length` 时不发布摘要、不推进 cursor；开启、关闭、供应商默认三种设置均覆盖。
6. 旧 CURRENT 的计划恢复、尾部边界、费用、Skill 恢复及缓存评测显式保留历史策略，原有断言继续通过。

相关代码通过 Ruff 检查；仅格式清理部分额外核对 AST 等价。当前工作区加载结果为 `a_fallback`、`deepseek-v4-flash`、`thinking=None`；没有读取或输出模型凭据。

外部框架摘要输出上限的对照及来源仍在[压缩长度对照](compaction-limits-retention-thinking.md)。该报告中的 bot 历史配置和测试数据保持原口径，并已加上本次变更说明。

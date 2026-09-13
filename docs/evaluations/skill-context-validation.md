# Skill 历史交付：实施与验证

验证日期：2026-09-10；实现提交：`875750c`。对应[首版实施方案](../designs/active-skill-layer-removal-plan.md)。
下列测试与模型结果为该次实施验收记录；后续文档修订没有重跑真实模型或更新历史数字。

## 已实现行为

新会话默认 `skills.context_mode="history"`，不产生 `ContextLayer.ACTIVE_SKILL` 条目。
`activate_skill`、Skill Catalog 和 `load_skill_resource` 保留。自动激活把完整正文放在真正的
Tool Result 中；显式加载在当前用户输入后追加不可授权的合成消息。同版完整历史交付可以复用，
正文不会经过通用头尾预览。控制工具 schema 在同一目录快照内保持稳定。

`RunSkillState` 持有本 Run 的独立绑定，冻结 Catalog 快照和已加载的正文版本。完成、取消、异常及持久化失败后关闭
状态并释放执行准入；另一个 Run 的绑定不受影响。历史正文仍可回放，但不会自动授权下一 Run
加载资源。磁盘 reload 只影响后续 Run，不替换本 Run 已绑定的正文。

消息与 `skill_deliveries` 来源侧表在同一个事务中提交，保存正文 blob 引用、版本哈希及实际消息哈希。
历史加载、按位置查询和 fork 保留来源；合成 Skill 消息不进入真实用户锚点或用户偏好归因。
运行中必要正文及其 Assistant Call / 同批 Tool Result 整组必留；每次执行请求和模型收尾前，
核对 Provider 序列化后的完整内容、工具协议及输入预算。

压缩覆盖活动正文时，从本 Run 绑定的原版 blob 在历史尾部恢复，不要求模型再调用读取工具。
恢复在相同游标上幂等。资源保护的是已交付视图，持续到下一次有效模型响应；资源原文件仍遵循
现有截断与按需读取规则。blob 缺失、损坏、无权限或最终内容不可见时停止执行。
本地装箱及 Provider 超限共用每次执行 step 的一次修复机会；失败后使用确定性收尾，避免循环。

## 会话升级与配置

SQLite schema 升为 v15。已有会话迁移为 `legacy`；新会话首次 Run 按配置选定布局并持久化。
重启或修改配置不会切换已有会话，fork 继承来源会话的模式。

```toml
[skills]
context_mode = "history"
```

默认配置下使用 `/new` 创建采用 history 布局的会话。若显式配置为 legacy，新会话也按该配置
确定布局。`/status` 的 `context.skill_context_mode` 显示实际布局，
`active_skills` 按会话查询本 Run 的绑定。兼容会话仍使用 `_legacy_skill_items()`；这次没有删除
旧布局读取能力，也不能直接回退到不识别合成历史来源的旧二进制。

## 确定性验证

```bash
.venv/bin/ruff check src tests scripts/run_skill_context_smoke.py
.venv/bin/pytest tests/unit tests/integration --disable-warnings --maxfail=3
```

结果：Ruff 通过；**452 passed, 7 skipped**，耗时 10.31 秒。新增 Skill 专项共 27 项，入口为
[Runtime 单测](../../tests/unit/test_skill_runtime.py)与[Agent 集成测试](../../tests/integration/test_skill_context.py)。

| 覆盖 | 检查结果 |
|---|---|
| 自动与显式加载 | 超过 14K 字符的正文中部、尾部完整；单份历史交付；无独立 Active Skill 层 |
| 重复加载、下一 Run、fork | 同版完整正文复用；新 Run 仍需重新绑定才能加载资源 |
| 连续压缩及 reload | 连续两次恢复原版本，合成正文不成为用户锚点 |
| 工具原子组与 Provider 适配 | 必留结果带上完整调用及同批结果；截短正文或移除配对调用均在采样前拒绝 |
| 预算及损坏引用 | 正文预算、完整请求预算不足不采样；引用丢失/损坏/撤销授权不继续执行 |
| 超限与资源恢复 | Provider 超限最多重试一次；尚未消费的资源视图在恢复期间保留 |
| 收尾和并发 | 无工具 finalizer 可恢复原文；取消或持久化失败释放本 Run，不清空另一个 Run |
| 存储 | 交付事务回滚、幂等键、消息哈希、v14 迁移、restart/fork 的布局与 blob 访问权 |

## 真实模型小规模对照

使用当前配置的 `deepseek-v4-pro`，固定 temperature=0、thinking=disabled。只发送临时生成的
审计手册与两个证据文件；可调用的业务工具为 `read_file`。每个布局、场景各一次，均追加一个
要求只回复 `PLAIN_852` 的无关新任务。压缩场景使用真实 `ContextCompactor` 和同一模型。

```bash
.venv/bin/python scripts/run_skill_context_smoke.py --live \
  --output .bot/benchmarks/skill-context-smoke.json
```

[归档 JSON](../data/skill-context-smoke-2026-09-10.json)保存逐请求 Provider 原始 usage、请求延迟、
工具名称、尾部标记出现数和最终答案。标记出现数不等同于逐字完整性证明；后者由 Runtime 门禁
与确定性测试验证。`legacy` 对照使用新的 Run 隔离和稳定工具定义，只比较正文布局，非旧二进制。

| 场景 | legacy | history | 观察 |
|---|---|---|---|
| 短手册自动激活 | 通过 | 通过 | 自动加载后，正文尾部标记由两条消息降为一条，history 无独立层 |
| 长手册显式加载 | 格式失败 | 通过 | legacy 读取事实正确，但在规定答案前增加解释；history 满足精确格式 |
| 长手册压缩后继续 | 通过 | 通过 | history 恢复一次原文，继续读取第二份证据并按尾部格式输出 |
| 各场景后的无关任务 | 3/3 通过 | 3/3 通过 | 均按新任务输出，未观察到旧格式干扰 |

本次脚本退出码为 1，因为它同时校验两个布局，而 legacy 的长手册格式未通过。
history 的三个审计场景及三个后续任务全部通过；单次样本不能证明普遍行为提升。

下表为每个场景的**全部请求合计**，包括无关后续 Run 和独立压缩请求。cached/uncached
来自 Provider 实际 usage，未使用本地估算替代。

| 场景 / 布局 | 输入 tokens | cached | uncached | 输出 tokens | 请求延迟合计（秒） |
|---|---:|---:|---:|---:|---:|
| 短自动 / legacy | 12,002 | 8,704 | 3,298 | 207 | 12.206 |
| 短自动 / history | 11,149 | 10,240 | 909 | 184 | 12.844 |
| 长显式 / legacy | 33,327 | 24,192 | 9,135 | 179 | 11.546 |
| 长显式 / history | 41,543 | 32,512 | 9,031 | 117 | 11.263 |
| 压缩续跑 / legacy | 34,715 | 24,448 | 10,267 | 324 | 14.783 |
| 压缩续跑 / history | 52,219 | 17,280 | 34,939 | 670 | 20.273 |

**不能据此宣称普遍节费。** legacy 先运行，缓存预热未隔离；没有经核验的价格配置，因此未计算
金额。显式正文在 history 的无关下一 Run 中仍占输入；压缩器也会处理历史中的长正文，随后恢复
又需回放原文。表中的输入增长体现了这些实际代价，是否值得进一步回收应另做成对成本评测。

## 保留边界

- 本次完成历史交付、Run 隔离和必要正文恢复；未实现通用历史投影、跨回滚去重或独立微裁剪。
- 新模式保证必要正文完整进入受支持 Provider 的请求，不能保证模型逐条服从所有规则。
- 旧会话继续使用兼容布局，旧布局不享有新模式的正文完整性门禁。
- 资源目录未整体冻结；本 Run 冻结的是 Catalog/正文及已经交付的资源视图。
- finalizer 恢复后若仍不满足预算，退回确定性收尾，不另开一轮无界修复。
- 尚无长工具循环、多 Skill 切换的成对真实成本样本；不能用此次 smoke 替代完整性能推广评测。

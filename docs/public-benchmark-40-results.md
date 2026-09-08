# 40 题公开基准结果与失败分析

这是执行中的记录，不是最终报告。2026-09-09 02:06（Asia/Shanghai）快照：
40 题中 5 题已完成真实模型运行并得到官方结果，3 通过、2 未通过。
其中 4 题实际执行了模型产物的验收测试，1 题因空补丁被官方直接记为未解决，未执行验收测试。
35 题尚无最终模型结果，其中 1 题被失败的官方参考解控制实验阻断，其他任务继续推进。
不能据此宣称 40 题已完成，也不能将 3/5 当作完整公开基准成绩。

范围、冻结规则、版本与限制见 [执行约定](public-benchmark-40-execution.md)。
源码基线为 `578f660`，模型为 `deepseek-v4-flash`。本报告中的协议探针只用于分析，未修改该冻结基线。
原始产物根目录为 `artifacts/public-benchmark/20260909-flash/`；
[证据清单](public-benchmark-40-evidence.json) 记录相对路径及 SHA-256，避免把后续复测覆盖成首次成绩。

## 当前逐题结果

| 任务 | 官方控制实验 | Bot 首次判分 | 说明 |
| --- | --- | --- | --- |
| Terminal / openssl-selfsigned-cert | oracle=1，nop=0，无异常 | reward=1 | 23 步完成；官方验收通过 |
| Terminal / custom-memory-heap-crash | oracle=1，nop=0，无异常 | reward=0；5 passed、1 failed | 第 9 步触发模型输出长度限制，尚未执行源码修复 |
| Terminal / large-scale-text-editing | oracle=0，nop=0，无 harness 异常 | 尚未运行 Bot | 官方参考解在验收脚本内超时；不能按模型失败计分 |
| SWE / astropy__astropy-12907 | gold resolved=true，negative resolved=false | resolved=true | 27 步完成；2 项 FAIL_TO_PASS、13 项 PASS_TO_PASS 均通过 |
| SWE / django__django-14017 | gold resolved=true，negative resolved=false | resolved=true | 46 步完成；2 项 FAIL_TO_PASS、147 项 PASS_TO_PASS 均通过 |
| SWE / sympy__sympy-18532 | gold resolved=true，negative resolved=false | 空补丁，resolved=false | 第 20 步触发输出长度限制；官方未执行模型补丁验收测试 |
| Terminal / multi-source-data-merger | oracle=1，nop=0 | Agent 安装/执行中 | 已进入主批次的下一题 |

其余题目保留在 [固定 20＋20 清单](../evals/public-regression-40-v1.json)，没有因缓存、耗时或失败而换题。
批次使用独立 attempt、官方 run_id 和进程记录继续执行；实时汇总位于产物目录的 `summary.json`。
原定 6 题冒烟目前完成 5 题真实模型运行；大文件编辑题仍因参考解超时而暂挂，其余任务先继续执行，未用另一题替换它。

## 如何判定通过

Terminal 以官方 verifier 的 reward 为准；无执行异常且 reward=1 才记为通过。
SWE 以官方 `report.json` 的 `resolved` 为准，检查问题修复所需的 FAIL_TO_PASS 和回归保护所需的 PASS_TO_PASS。
补丁能应用、Bot 自称修好了、内部状态为 completed，均不足以证明通过。

每题还检查已知正确方案和负向控制。Astropy 当前控制实验中，官方补丁修复了 2 项 FAIL_TO_PASS，
并保留 13 项 PASS_TO_PASS；无关补丁成功应用后，2 项 FAIL_TO_PASS 仍失败，13 项 PASS_TO_PASS 保持通过。
这证明此实例确实在检查修复，而不是仅检查容器能启动或补丁能应用。

参考解无法通过的实例暂不进入有效模型通过率分母，单独报告环境或验收机制问题；也不会从 40 题清单消失。
LLM 不参与上述通过与否的裁决。失败原因由轨迹审查与对照实验分析，需标明事实、推断与尚缺的证据。

## 失败 1：custom-memory-heap-crash

### 已确认的事实

- 官方参考解可通过，空操作不能通过；首次 Bot 结果中 Release 程序仍以退出码 -11 崩溃。
- Bot 进行了文件检查、编译和运行，但结构化工具记录中尚无对 `/app/user.cpp` 的修改操作。
- 第 8 步返回 2 个原生工具调用，reasoning 字段有出现在流中，但内容为空。
- 第 9 步返回 8192 个 completion tokens，`finish_reason=length`，原生工具调用数为 0；
  正文中出现 DSML 工具调用标记。Bot 将其保留为正文，结束为 `limit_reached / model_output_limit`。
- 最终总结请求同样没有执行修复动作。官方测试结果为 5 passed、1 failed，失败项为 `test_release_build_runs_without_crash`。

这是一道未完成修复的失败，不能描述成“模型提交了错误补丁”。8192 是此次响应实际达到的输出长度；
冻结配置的 `max_output_tokens` 为 None，Bot 未在该请求中显式指定 8192。

### 已定位的 Bot 协议问题

当前实现将空 reasoning 与缺失 reasoning 合并：

1. `core/agent.py` 用 `reasoning_text or None` 保存助手消息。
2. `ChatMessage.to_openai()` 仅在 reasoning 非空时回传此字段。
3. DeepSeek Provider 遇到历史工具调用缺少 reasoning 时，会强制切换到 `thinking=disabled`。

三个最小 API 探针使用同一工具历史，仅改变 reasoning 字段与 thinking 参数：

| 历史工具调用中的 reasoning | thinking | HTTP 结果 |
| --- | --- | --- |
| 字段缺失 | 服务默认 | 400，要求回传 reasoning_content |
| 显式空字符串 | 服务默认 | 200，正常回答 |
| 字段缺失 | disabled | 200，正常回答 |

因此，“合法空字段丢失，导致不必要的模式切换”是已确认的兼容性缺陷。
服务要求带 tools 的请求回传历史 reasoning，见 [DeepSeek Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)。
修复方向是保留空值与缺失的区别，并补上该协议状态的回归测试；不能简单删除对真正缺失 reasoning 的兼容处理。

### 因果边界与复测

使用失败前的记录重建一轮请求，分别重放当前 disabled 行为和保留空 reasoning 的行为，各 2 次。
四次都获得了原生工具调用，没有再出现 DSML 正文；保留空 reasoning 的两次恢复了非空 reasoning 输出。
这些是单轮协议实验，没有执行返回的工具，也未经过官方整题验收。重建请求未包含原请求的完整运行提示和动态工具选择快照。

所以目前不能断言模式切换必然导致此次 DSML/长度失败，更不能断言修好该字段后整题就会通过。
Astropy 的成功运行也经历了 reasoning 为空的工具轮次，说明这一现象不足以单独预测整题失败。
当前归因是“协议兼容问题已确认；此次失败的直接成因仍需整题对照”，不是“纯模型能力不足”。
后续需要保持模型、题面、源码其余部分和验收不变，分别重复原配置与仅修复字段回传的候选配置；
原始失败会继续保留。是否提高输出上限应作为独立变量，避免一次改动多个因素后无法归因。
同配置整题复测 attempt 2 在模型启动前因 Ubuntu 软件源连接失败而结束，没有产生新的模型成绩。
安装日志显示 `apt-get update` 在 9 分 8 秒内仅下载 13.3 MB，随后 universe Packages 连接失败。
临时容器经已有宿主机代理访问同一软件源得到 HTTP 200，读取 64 KiB 用时 1.6 秒，容器已退出清理。

后续原版本 attempt 3 与候选版本 attempt 4 使用相同代理设置顺序执行，继续复用首次通过的正反例控制，
保持镜像 ID、题面、模型、步数与时间限制一致。二者使用官方的 1 CPU / 2 GiB 资源上限，
与主批次任务同时运行，期间跨越 SWE 与 Terminal 任务，需保留这一负载条件。候选只改变 reasoning 空值的协议处理，输出上限仍未调整。
attempt 3 已由官方判为 reward=0，仍是 Release 崩溃，5 passed、1 failed。
该次模型在第 22 步达到输出长度限制，最后一轮长正文没有 DSML 标记；此现象与首次的 DSML 正文应分开记录。
模型结束后、容器清理前已保存最终 `user.cpp`，其 SHA-256 为
`a231a3f4a4f524d1123ce4a6aa9d12a4dec013a3676b28437142365c28c80732`。

### 隔离候选的回归验证

[诊断补丁](../evals/experiments/deepseek-empty-reasoning.patch) 和
[实验清单](../evals/experiments/deepseek-empty-reasoning.json) 已保存；补丁未应用到主工作区的 `src/` 或正在执行的冻结基线。
候选记录官方 DeepSeek 请求实际选择的 thinking 模式，仅在 thinking 模式的工具轮次中保留空 reasoning；
对已关闭 thinking 或旧历史中缺失 reasoning 的情况继续使用原有兼容处理。

- 新增 9 个参数化回归用例：冻结旧版 5 failed、4 passed，无测试环境错误。
- 候选版本的 Provider 和 Agent loop 回归共 62 passed，无失败或跳过。
- 验证了导入模块确实来自各自源码目录，wheel 中相关模块字节与候选源码一致，补丁能应用到基线。

这些检查证明候选覆盖了已定位的协议缺陷。整题对照尚在执行，不能把上述单元/集成测试通过写成 benchmark 已通过。
候选的真实 API 轨迹已出现空 reasoning 工具轮次：第 8、10 步为空，其后的第 9、11 步分别恢复非空 reasoning，
记录的 thinking 模式保持 enabled。这验证了候选机制确实在真实任务中被触发，整题得分仍需等待官方验收。

## 失败 2：sympy__sympy-18532

官方参考补丁通过，无关补丁未通过。Bot 运行到第 20 步时得到 `finish_reason=length`，
随后导出的 `model_patch` 长度为 0。官方汇总为 `empty_patch_instances=1`、`completed_instances=0`、`resolved_instances=0`。
因此此项是“没有提交修复”，不能写成“提交的修复未通过某条官方测试”。

运行记录显示模型完成了源码阅读、类型行为检查、Git 历史查询以及一次依赖安装，没有发出源码写入操作。
空补丁与操作记录一致，目前没有证据表明补丁导出器丢失了修复。
默认 `python` 最初缺少 mpmath，模型执行 `pip install mpmath` 后相关检查可以运行；
仍需使用相同镜像核对默认工具环境与官方 testbed 环境是否一致，不能仅凭“安装成功”认定环境完全等价。

第 3 步出现空 reasoning 的工具调用，随后 reasoning 持续为空；第 20 步输出约 3.4 万字符的正文，
没有原生工具调用，触发了输出长度限制。这与内存堆失败共享协议模式切换与长度限制现象，但仍不是充分的单因证明。
当前归因为“未形成修复，受模型输出限制终止；已知协议缺陷可能参与”，模型能否在兼容修复后解决该 issue 尚待整题对照。
相同 digest 的镜像正在为诊断恢复；原始空补丁结果保留在首次成绩中。

## 阻断 1：large-scale-text-editing

官方 oracle 的 5 项验收中 3 passed、2 failed。首个失败为 `test_apply_macros_runs`：
验收器在 `subprocess.run(..., timeout=600)` 中等待 Vim 超时。随后 `expected.csv` 未生成，导致第二项比较失败。
后者是前一项超时的后果，不能另行归因为数据集丢失。

容器在 ARM64 Mac 上通过 QEMU 执行 AMD64 镜像；超时期间 Vim 持续消耗约一个 CPU 核。
Harbor 本身没有抛出异常，但 reference reward=0。仅检查退出码或“无 harness exception”会漏掉这类无效评测环境。

已确认本机装有 Rosetta，Docker Desktop 使用 Apple Virtualization Framework，但 Rosetta 选项关闭。
QEMU 性能是待验证的原因，尚未有另一执行后端的对照，不能将其写成已证实的唯一根因。
Docker 官方提供 [Rosetta 加速选项](https://docs.docker.com/desktop/settings-and-maintenance/settings/)。
切换需要重启共享 Docker 环境，已请求用户确认在空闲窗口临时启用、结束恢复；未获确认前保持现有设置。
下一步是在同题、同镜像、同验收脚本的条件下复跑 oracle。放宽官方测试内的 600 秒限制只能作为诊断，不能冒充原始标准分数。

## 尚未完成

- 剩余题目的真实模型运行与官方判分，以及被参考解失败阻断题目的环境复验。
- 所有失败题的逐题审查；当前两个问题不能外推到尚未运行的其他题目。
- 内存堆题的整题重复和单因素候选对照；当前单轮探针不能替代它们。
- 整批成本、失败类型分布、资源清理审计和最终报告。缓存/峰谷感知成本只是估算，不是账单。

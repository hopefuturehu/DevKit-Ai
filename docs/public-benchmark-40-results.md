# 40 题公开基准结果与失败分析

这是执行中的记录，不是最终报告。2026-09-09 03:56（Asia/Shanghai）快照：
40 题中 8 题已完成首次真实模型运行并得到官方原始结果：5 通过、3 个零分或未解决。
其中 SAM 的零分来自测试收集失败，实际执行 0 项测试，应单列为无效评测。
因此当前可用于模型结果分析的是 5 通过、2 未通过：6 题执行了产物验收测试，
另 1 题因空补丁被官方直接记为未解决。不能把无效评测混入模型失败率。
32 题尚无首次最终模型结果，其中 2 题被失败的官方参考解控制实验阻断，其他任务继续推进。
不能据此宣称 40 题已完成，也不能将 5/7 当作完整公开基准成绩。
另有大文本题的环境变体完成真实模型运行并通过，实际已覆盖 9 个不同题目；
该结果单列，不与原镜像首次基线合并。

范围、冻结规则、版本与限制见 [执行约定](public-benchmark-40-execution.md)。
源码基线为 `578f660`，模型为 `deepseek-v4-flash`。本报告中的协议探针只用于分析，未修改该冻结基线。
原始产物根目录为 `artifacts/public-benchmark/20260909-flash/`；
[证据清单](public-benchmark-40-evidence.json) 记录相对路径及 SHA-256，避免把后续复测覆盖成首次成绩。

## 当前逐题结果

| 任务 | 官方控制实验 | Bot 首次判分 | 说明 |
| --- | --- | --- | --- |
| Terminal / openssl-selfsigned-cert | oracle=1，nop=0，无异常 | reward=1 | 23 步完成；官方验收通过 |
| Terminal / custom-memory-heap-crash | oracle=1，nop=0，无异常 | reward=0；5 passed、1 failed | 第 9 步触发模型输出长度限制，尚未执行源码修复 |
| Terminal / large-scale-text-editing | oracle=0，nop=0，无 harness 异常 | 原镜像尚未运行 Bot | 官方参考解超时；环境变体已通过，见单独记录 |
| SWE / astropy__astropy-12907 | gold resolved=true，negative resolved=false | resolved=true | 27 步完成；2 项 FAIL_TO_PASS、13 项 PASS_TO_PASS 均通过 |
| SWE / django__django-14017 | gold resolved=true，negative resolved=false | resolved=true | 46 步完成；2 项 FAIL_TO_PASS、147 项 PASS_TO_PASS 均通过 |
| SWE / sympy__sympy-18532 | gold resolved=true，negative resolved=false | 空补丁，resolved=false | 第 20 步触发输出长度限制；官方未执行模型补丁验收测试 |
| Terminal / multi-source-data-merger | oracle=1，nop=0 | reward=1 | 10 步完成，官方验收通过 |
| Terminal / sparql-university | oracle=1，nop=0 | reward=1 | 11 步完成，官方验收通过 |
| Terminal / sam-cell-seg | oracle=1，9 passed；nop=0，9 failed | 原始 reward=0；无效评测 | 95 步 completed；依赖下载失败后测试收集报错，0 项执行；同产物复验中 |
| Terminal / rstan-to-pystan | oracle=0，nop=0；各 1 passed、5 failed | 尚未运行 Bot | 参考解安装依赖遇到 HTTP 502，未生成输出文件 |

其余题目保留在 [固定 20＋20 清单](../evals/public-regression-40-v1.json)，没有因缓存、耗时或失败而换题。
批次使用独立 attempt、官方 run_id 和进程记录继续执行；实时汇总位于产物目录的 `summary.json`。
原定 6 题冒烟目前完成 5 题真实模型运行；大文件编辑题仍因参考解超时而暂挂，其余任务先继续执行，未用另一题替换它。

## 如何判定通过

Terminal 保留官方 verifier 的原始 reward，并另外检查验收是否实际执行。
无执行异常、reward=1 且验收证据有效才可报告通过；已知 0 项测试执行的结果单列为无效评测，即使原始 reward=1 也不接受。
没有结构化报告的旧式验收器标记为待核查，不能仅因缺少该文件就断定 0 项测试。
SWE 以官方 `report.json` 的 `resolved` 为准，检查问题修复所需的 FAIL_TO_PASS 和回归保护所需的 PASS_TO_PASS。
补丁能应用、Bot 自称修好了、内部状态为 completed，均不足以证明通过。

每题还检查已知正确方案和负向控制。Astropy 当前控制实验中，官方补丁修复了 2 项 FAIL_TO_PASS，
并保留 13 项 PASS_TO_PASS；无关补丁成功应用后，2 项 FAIL_TO_PASS 仍失败，13 项 PASS_TO_PASS 保持通过。
这证明此实例确实在检查修复，而不是仅检查容器能启动或补丁能应用。

参考解无法通过的实例暂不进入有效模型通过率分母，单独报告环境或验收机制问题；也不会从 40 题清单消失。
参考解与负向控制均通过检查，也不能保证后续模型产物的验收容器不会遇到新的依赖或网络故障。
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
后续整题对照已获得正向证据：只调整协议处理的候选版本通过了全部 6 项官方测试。
模型在相同题目和输出配置下确实能够完成修复，因此优先修复 Agent 兼容问题有直接依据。
对此次失败的贡献归因置信度为中等：当前有两次有效原版失败、一次候选成功，仍受模型采样随机性影响，不能据此估计普遍提升幅度。
原始失败继续保留。是否提高输出上限属于另一个变量，本轮没有调整它。
同配置整题复测 attempt 2 在模型启动前因 Ubuntu 软件源连接失败而结束，没有产生新的模型成绩。
安装日志显示 `apt-get update` 在 9 分 8 秒内仅下载 13.3 MB，随后 universe Packages 连接失败。
临时容器经已有宿主机代理访问同一软件源得到 HTTP 200，读取 64 KiB 用时 1.6 秒，容器已退出清理。

原版本 attempt 3 与候选版本 attempt 4 使用相同代理设置顺序执行，复用首次通过的正反例控制，
保持镜像 ID、题面、模型、步数与时间限制一致。二者使用官方的 1 CPU / 2 GiB 资源上限，
与主批次任务同时运行，期间跨越 SWE 与 Terminal 任务，需保留这一负载条件。候选只改变 reasoning 空值的协议处理，输出上限仍未调整。
attempt 3 已由官方判为 reward=0，仍是 Release 崩溃，5 passed、1 failed。
该次模型在第 22 步达到输出长度限制，最后一轮长正文没有 DSML 标记；此现象与首次的 DSML 正文应分开记录。
模型结束后、容器清理前已保存最终 `user.cpp`，其 SHA-256 为
`a231a3f4a4f524d1123ce4a6aa9d12a4dec013a3676b28437142365c28c80732`。

| 整题运行 | 协议处理 | Agent 结束状态 | 官方结果 |
| --- | --- | --- | --- |
| attempt 1，首次基线 | 原版 | 第 9 步输出长度耗尽 | reward=0，Release 崩溃 |
| attempt 3，共同代理路径对照 | 原版 | 第 22 步输出长度耗尽 | reward=0，Release 崩溃 |
| attempt 4，共同代理路径候选 | 保留空 reasoning | 27 步 completed | reward=1，6 项全部通过 |

三次记录的服务 fingerprint 相同。两次原版的最大单轮 completion 为 8192 tokens，候选为 4725；
候选没有依靠提高输出上限通过。其最终源码与原版复测不同，已保存 SHA-256
`5ee212b6bc82804b6a71f92922c24f26878671c75c10226c9179d1e834878be3`。
这次成功属于诊断复测，不替换首次基线的失败，也不增加已覆盖的题目数量。

### 隔离候选的回归验证

[诊断补丁](../evals/experiments/deepseek-empty-reasoning.patch) 和
[实验清单](../evals/experiments/deepseek-empty-reasoning.json) 已保存；补丁未应用到主工作区的 `src/` 或正在执行的冻结基线。
候选记录官方 DeepSeek 请求实际选择的 thinking 模式，仅在 thinking 模式的工具轮次中保留空 reasoning；
对已关闭 thinking 或旧历史中缺失 reasoning 的情况继续使用原有兼容处理。

- 新增 9 个参数化回归用例：冻结旧版 5 failed、4 passed，无测试环境错误。
- 候选版本的 Provider 和 Agent loop 回归共 62 passed，无失败或跳过。
- 验证了导入模块确实来自各自源码目录，wheel 中相关模块字节与候选源码一致，补丁能应用到基线。

这些检查证明候选覆盖了已定位的协议缺陷；整题通过的依据另来自上表的官方验收。
候选的真实 API 轨迹已出现空 reasoning 工具轮次：第 8、10 步为空，其后的第 9、11 步分别恢复非空 reasoning，
记录的 thinking 模式保持 enabled；全部 27 个正常模型轮次均保持 enabled。
这验证了候选机制确实在真实任务中被触发。SymPy 失败题的同候选交叉验证也已通过官方验收，详见下节；
该次运行仍出现了另一次协议错误，不能把候选描述成完整解决了所有 reasoning 兼容问题。

## 失败 2：sympy__sympy-18532

官方参考补丁通过，无关补丁未通过。Bot 运行到第 20 步时得到 `finish_reason=length`，
随后导出的 `model_patch` 长度为 0。官方汇总为 `empty_patch_instances=1`、`completed_instances=0`、`resolved_instances=0`。
因此此项是“没有提交修复”，不能写成“提交的修复未通过某条官方测试”。

运行记录显示模型完成了源码阅读、类型行为检查、Git 历史查询以及一次依赖安装，没有发出源码写入操作。
空补丁与操作记录一致，目前没有证据表明补丁导出器丢失了修复。
默认 `python` 最初缺少 mpmath，模型执行 `pip install mpmath` 后相关检查可以运行。
恢复相同 digest 的镜像后，已在不传入模型凭据的临时容器中确认：默认 Python 是 `/opt/miniconda3/bin/python` 3.11.5，
没有 mpmath；官方 testbed 的 Python 是 `/opt/miniconda3/envs/testbed/bin/python` 3.9.20，mpmath 已就绪。
当前 Worker 启动命令未激活 testbed，模型工具继承了默认 PATH。这是已确认的环境准备缺陷。
通过的官方参考解控制不能证明 Agent 使用了同一个依赖环境。

修复方向是在保持 Bot 自身独立 Python 运行时的同时，为任务工具激活官方 testbed 环境，
并在模型开始前检查默认解释器和项目依赖导入。不能用安装最新依赖替代复用实例准备好的版本。
本例模型后来成功补装依赖，因此尚不能认定该缺陷直接导致了最终空补丁；要与协议问题分开验证。
另一个无模型探针执行了冻结版本 `LocalExecutionTarget._safe_environment` 的实际方法代码，
再用过滤后的环境启动容器内项目 Python：默认环境缺少 mpmath，激活 testbed 后则使用 Python 3.9.20、
mpmath 1.3.0，并从 `/testbed/sympy` 导入项目。这个最小实跑验证了激活方案与现有环境过滤兼容，
尚不等同于修改 Worker 后的完整集成测试。该诊断镜像和临时容器已清理，保留了版本与结果证据。

第 3 步出现空 reasoning 的工具调用，随后 reasoning 持续为空；第 20 步输出约 3.4 万字符的正文，
没有原生工具调用，触发了输出长度限制。这与内存堆失败共享协议模式切换与长度限制现象，但仍不是充分的单因证明。
首次归因为“未形成修复，受模型输出限制终止；已知协议缺陷可能参与”。
相同 digest 镜像上的协议候选 attempt 2 已完成，保持原有工具环境，复用原有正反例控制。
该次导出 2410 字符的实现补丁；官方判定 resolved=true，2 项 FAIL_TO_PASS、28 项 PASS_TO_PASS 全部通过。
这证明相同模型能在该问题上形成可验收的修复，支持 Agent 协议缺陷参与原始失败的解释。
原始空补丁结果仍保留在首次成绩中，复测不增加题目覆盖数，也不用于估计普遍提升幅度。

候选在 118 个正常模型轮次中一直记录为 thinking=enabled，9 个工具轮次的 reasoning 为空，
但空字段被保留；最大单轮 completion 为 4418 tokens，没有提高输出配置。
总消耗为 6,237,427 input tokens、93,547 output tokens，明显高于原版提前终止的运行。
因此该对照证明的是在相同步数、时间与输出配置上限内能形成修复，不是相同实际 token 消耗下的提升。

### 复测暴露的残留协议错误

模型已经写入实现补丁，但第 119 步请求返回 reasoning_content 缺失相关 HTTP 400，
Agent 最终状态为 failed。适配器仍成功导出了已写入的补丁，并通过官方验收，所以必须分别报告“产物通过”和“运行发生协议错误”。
不能因为最终 Agent 状态为 failed 就否认官方通过，也不能用官方通过掩盖这次协议错误。

保存的 118 条工具调用助手消息均具有 reasoning 字段。此次未保存被服务拒绝的精确出站请求，
因此还不能定位是请求构造的其他分支还是服务侧行为导致了错误，也不能仅因之前发生过上下文压缩就归因于压缩。
后续诊断需要记录不含凭据的请求结构和字段存在性，并重放被拒绝请求；单靠持久化 transcript 不足以代替实际 payload。

### Git 历史的有限复核

这两次运行查询过 Git 历史。轨迹显示镜像 HEAD 为一个名为 `SWE-bench` 的提交，后面紧接题目的目标提交 `74227f900b...`。
所安装的官方 harness 4.1.0 构建脚本会重置到目标提交、删除远端和较新的标签、清理 reflog 与不可达对象，
然后把安装造成的改动提交为 `SWE-bench`。因此，看到一个日期较新的同名构建提交，本身不能证明模型接触到了未来修复。
已审查的 Git 工具调用没有显示读取未来修复代码；但已清理镜像的完整 Git 对象没有逐一核验，不能据此宣称所有镜像都不存在历史泄露。
已为后续选定的 SWE 镜像准备独立审计，检查 Git HEAD、目标提交之外的可达历史，以及默认和 testbed Python 的项目导入情况。
审计使用禁用网络的临时容器，不调用模型；当前主批次仍在 Terminal 题目上，这个新增镜像探针尚未实跑。

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

为在不重启 Docker 的条件下继续调查，已在一次性 ARM64 Alpine 容器中编译静态 Vim 9.0.1378，
并验证它可以在原 AMD64 任务容器中直接运行。隔离的派生镜像只替换 Vim 可执行文件，
题面、参考解和全部验收文件的字节保持一致，600 秒限制保持不变。
该变体已得到 oracle=1、nop=0，无执行异常；参考解的全部 5 项测试通过，pytest 总用时 83.42 秒。
原版本 Bot 在这个变体上 24 步 completed，reward=1，全部 5 项测试通过，pytest 总用时 74.98 秒。
消耗 405,824 input tokens、26,600 output tokens。最终 `apply_macros.vim` 已在容器清理前保存，
SHA-256 为 `7ab23c6585ed3497c60b1aaf426de53ff1926906aa3db09b9ddc9395d803fdc3`。
同一模型产物在原 AMD64 镜像中的复验也已结束，没有再次调用模型：reward=0，3 passed、2 failed，
首个失败仍为 Vim 的 600 秒超时，随后 expected.csv 缺失是超时的后果。pytest 总用时 601.42 秒。
这次固定产物、固定验收文件的对照进一步证明，此脚本的验收差异来自执行环境，不能解释为模型没有生成正确的转换脚本。
该结果不替换官方参考解控制的原始失败记录，也不增加模型调用次数或题目覆盖数。
该二进制与原 Debian Vim 的补丁集、构建参数和 C 库不同，因此这是环境诊断变体，不能冒充原镜像成绩，
即使性能改善也不能单凭这一实验断定差异完全来自 QEMU。
派生镜像及该变体的临时容器已清理，保留源码、构建信息、可执行文件、模型产物和验收证据供复核。

## 无效评测：sam-cell-seg

原始参考解执行了 9 项测试并全部通过；空操作执行了同样 9 项测试并全部失败，正反例控制有效。
Bot 完成了 95 步，没有 Agent 执行错误，生成的 `convert_masks.py` 已在容器清理前保存。
最终脚本 SHA-256 为 `0925b4575d350d9d8172199553822c199e1c127562dcff3e34f19fff4bd6c2d9`，
采集记录同时关联最终 Agent 结果文件，区别于此前运行中保存的临时快照。

官方 `test.sh` 已包含 `apt-get install -y curl git libgl1`，问题不是漏写依赖。
本次安装在下载 `libgl1`、`libwayland-client0` 和 `mesa-vulkan-drivers` 时遇到 Debian 软件源连接失败，APT 返回 100。
脚本没有在这一失败处终止，仍继续启动 pytest；`cv2` 导入时找不到 `libGL.so.1`，导致测试收集报错。
CTRF 报告中 tests、passed、failed 均为 0，Harbor 无 exception，reward 仍为 0。
这证明原始零分没有验收模型脚本的功能，不能归因为分割算法或模型能力不足。

正在使用相同镜像 digest、原样模型脚本和未修改的官方验收文件复验，不再次调用模型。
唯一准备变化是在验收前经已有宿主机代理重试安装必需系统依赖，并用实际动态加载检查确认 `libGL` 可用。
目前依赖加载检查已通过，正式验收正在运行。复验结果将单列，保留首次零分及其无效原因。
该实验会重新生成 CSV 等运行产物，因此验证的是冻结脚本在就绪环境中的行为，不是逐字节复验首次容器内的全部文件。

后续 harness 应让依赖安装失败立即中止并分类为环境错误，并在评分报告中检查实际执行的测试数量。
单靠 reward、进程退出或 Harbor exception 均不够。本轮先修正结果汇总，不改变正在运行的冻结 Bot。

## 阻断 2：rstan-to-pystan

参考解和空操作均为 reward=0，均执行了 6 项测试：1 passed、5 failed。
参考解日志显示 Ubuntu 的 `librhash0` 下载返回 HTTP 502，APT 安装未完成。
后续 `add-apt-repository` 命令不存在，参考解在生成分析脚本和 CSV 之前结束。
5 项失败均关联所需输出文件缺失，不能写成模型估计的参数不准确；这个任务尚未调用模型。

原脚本用了 `set -e`，但首次安装命令位于 `&&` 链中，因此该下载失败未立即退出，
直到下一个缺失命令才结束。这也说明“参考解无 Harbor exception”不代表依赖准备成功。
下一步是保持参考解计算逻辑及验收不变，经现有代理复验安装和参考解；原始失败保留，不换题。

## 报告统计的校验

[汇总脚本](../scripts/summarize_public_benchmarks.py) 修正了压缩用量遗漏：
使用每次压缩 API 请求的详细 usage，避免把随后发布的累计运行用量当作另一次响应；
旧格式只有压缩总量时保留 token，但将缺少缓存明细的费用估算标为未知。
当前 12 次已完成 Bot 运行的汇总 input/output tokens 均与其独立结果文件逐项一致。
这里统计的是 Bot 运行，独立 API 探针尚不包含在这组对账范围内；缓存与峰谷价格估算仍不是账单。

脚本也会显示已有模型事件、尚未形成最终报告的 Terminal 运行；结果文件缺失不会被直接当作“模型未启动”。
后续 attempt 按数字排序，只有 attempt 1 属于冻结首次基线，后续成功复测不会自动填补首次基线。
汇总新增 `baseline_scoring_counts`，将已知无效验收单列，原始 `baseline_counts` 和 reward 保持不变。
11 项回归检查覆盖压缩计量、重复 usage、两类基准的缺失终态、未写完的事件行、复测隔离、
0 项测试、全部跳过以及缺少结构化报告的区别。非空报告仅证明有测试执行，仍需结合日志审查环境故障。

## 尚未完成

- 剩余题目的真实模型运行与官方判分，以及被参考解失败阻断题目的环境复验。
- SAM 冻结脚本的有效复验、RStan 参考解依赖故障的复验，以及其余失败题的逐题审查。
- 协议候选残留 HTTP 400 的请求级定位，以及 SWE testbed 激活缺陷的独立验证；两道题的成功对照不能替代全套分析。
- 整批成本、失败类型分布、资源清理审计和最终报告。缓存/峰谷感知成本只是估算，不是账单。

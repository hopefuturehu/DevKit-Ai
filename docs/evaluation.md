# 通用任务评测

`bot eval run` 在新的 fixture 副本中执行每个 Case，再根据文件、结构化数据和实际执行轨迹独立验收。
它提供可接入公开任务集或自建任务的基础设施；当前两个 `generic.jsonl` Case 仍是关键词烟测，不能代表通用能力分数。

## 运行

```bash
bot eval run evals/artifacts.jsonl \
  --artifacts-dir .bot/evals \
  -o .bot/eval-artifacts.jsonl
```

[`evals/artifacts.jsonl`](../evals/artifacts.jsonl) 是一个可直接使用的产物验收示例：
读取给定数组，生成包含数量、总和、平均值的 JSON，同时保留输入文件。
结果必须满足具体数值和类型约束；仅在回答中声称完成无法通过。
执行这些命令会使用配置的真实模型及其预算。本阶段的实现验证使用离线 Provider 和本地 Docker，没有调用付费模型。

模型、预算、工具权限及 Skill 配置从 CLI 的当前工作区（或 `bot -C` 指定目录）加载；
`--config` 仍可指定配置文件。`--disable-skills` 可运行关闭 Skill 的对照组。
全部 Case 通过时退出码为 0；任一 `fail` 或 `error` 为 1。
单个 Case 执行或验收异常会留下结果，并继续下一 Case；JSONL 格式或字段定义无效则在加载时拒绝整份文件。

## Case 与隔离

每行是一个 JSON 对象，`id` 在文件内唯一。`prompt` 是交给 Agent 的任务描述。

| 字段 | 含义 |
|---|---|
| `workspace` | 相对于 JSONL 的输入 fixture 目录，默认 `.`；不再直接在这个目录运行 |
| `fixture_files` | 需要复制的相对文件/目录列表；省略表示复制允许的全部输入，`[]` 表示空工作区 |
| `memory_fixture` | 可选的初始记忆目录，相对于 JSONL；复制后使用 |
| `state_fixture` | 可选的初始 SQLite 数据库，相对于 JSONL；只读备份后使用 |
| `max_snapshot_bytes` | 单份快照或种子数据库的大小上限，默认 64 MiB，最大 1 GiB；快照另限 10000 个条目 |
| `explicit_skills` | 本次显式激活的 Skill；关闭 Skill 的对照组会忽略 |

每次执行有独立的临时 Git 工作区、SQLite 状态库、记忆目录和子 Agent worktree 目录。
默认不继承会话、审批记录和用户 Agent 目录；只有显式 `state_fixture` 会引入数据库中的历史状态。
配置中的 Skill 和项目 Agent 定义会复制到本次工作区并记录哈希。
配置中指向共享状态和记忆的路径会被覆盖为本次路径。

fixture 不复制 `.git`、`.bot`、`.venv`、`venv`、`node_modules`、Python/测试/lint 缓存、
`artifacts`、`dist`、`build`、`.DS_Store` 和 `.env*`（`.env.example` 除外）；显式选择也不会绕过这些排除项。
输入中的符号链接、特殊文件、越界相对路径会被拒绝。验收脚本目录和显式种子来源从任务输入中排除。
依赖应由任务准备流程或验收镜像提供，不复制开发机现成的虚拟环境。

Agent 执行仍使用本地执行器和 `workspace_only` 权限策略，**没有变成操作系统级沙箱**。
这里的隔离用于消除旧产物、共享状态和验收脚本污染；运行不可信代码的公开任务时，应将整个 Bot 进程放入任务容器，
或使用已有 [Terminal-Bench](terminalbench-evaluation.md) / [SWE-bench](swebench-evaluation.md) 容器入口。

## 验收条件

| 字段 | 判定规则 |
|---|---|
| `expected_status` | 运行状态，默认 `completed`；完成状态本身不足以让无断言 Case 通过 |
| `final_contains` / `final_not_contains` | 最终回答的文本包含/排除条件，仅适合基本格式烟测 |
| `files_contain` | 文件路径到文本片段列表；必须是普通文件，默认要求本次新增或内容改变 |
| `files_contain_require_change` | 默认 `true`；检查已有输入时可以显式设为 `false` |
| `required_changes` / `forbidden_changes` | 至少存在/不存在匹配路径的最终变化，包括新增、删除、内容或权限变化 |
| `verifiers` 中 `kind: json` | 对文件执行 JSON Schema 验收，默认 `fresh: true`，要求本次新增或内容改变 |
| `verifiers` 中 `kind: command` | 在独立 Docker 容器中执行受信任验收器 |
| `expected_tools` | 根运行中确实请求并成功完成的工具，不接受仅请求、失败或仍在运行的结果 |
| `tool_results` | 按工具名、请求参数、终态、成功标志、退出码和次数检查具体执行结果 |
| `forbidden_tools` | 根运行中禁止请求的工具，即使请求后来失败也会违反条件 |
| `expected_skills` | 根运行激活的 Skill；关闭 Skill 的对照组忽略此项 |
| `max_tool_calls` / `max_approval_requests` | 根运行的工具请求和审批次数上限 |

所有条件必须同时满足。没有任何验收条件的 Case 返回 `error`，不会默认为通过。
JSON Schema 在运行前检查，只允许文档内部引用；不联网取 Schema。非法 JSON、`NaN` 和类型/数值不符都不能通过。
重复写入相同内容、仅修改时间戳或权限，不满足产物的“内容改变”条件。

变化路径使用 Python `fnmatchcase` 匹配完整相对路径，`*` 可跨越 `/`，`**` 可匹配所有路径。
最终快照忽略根目录的 `.bot` 和 `.git` 运行状态目录；其余新增路径（包括嵌套 `.bot`）会参与检查。
变化断言比较执行前与清理后的最终快照，不检测已恢复原状的中间修改。
符号链接作为变化记录，不跟随读取；文件/JSON 验收不接受链接，命令验收遇到链接返回不支持的错误。

工具请求通过 Call ID 与实际 `tool.result` 配对，校验根 `session_id` 和 `run_id`；子 Agent 事件不能替代根工具结果。
命令的成功终态还必须有退出码 0；受管进程启动后的 `running` 结果要等到同一 process ID 的后续终态才能算完成。
例如下面条件要求至少一次以指定参数完成的命令：

```json
{
  "tool_results": [{
    "name": "run_command",
    "arguments_contain": {"argv": ["pwd"]},
    "success": true,
    "status": "completed",
    "returncode": 0,
    "min_count": 1
  }]
}
```

`arguments_contain` 是顶层参数子集的精确匹配。负向测试可以显式要求 `success: false`、`status: failed` 和非零退出码。
工具成功只证明执行行为，任务正确性仍应通过文件或独立测试验收。

## 容器命令验收器

Case 中声明验收器，不把它放入 Agent 的提示或任务工作区：

```json
{
  "verifiers": [{
    "kind": "command",
    "tests": "verifiers/answer",
    "image": "alpine:3.20",
    "argv": ["/bin/sh", "/verifier/check.sh"],
    "timeout_seconds": 30
  }]
}
```

`tests` 相对于 JSONL，必须是评测维护者提供的非空目录，不能包含整个 fixture。
其中 `check.sh` 可以是：

```sh
#!/bin/sh
if [ "$(cat /workspace/answer.txt 2>/dev/null)" = "42" ]; then
  printf '{"passed":true}' > "$BOT_EVAL_RESULT"
else
  printf '{"passed":false,"message":"answer must equal 42"}' > "$BOT_EVAL_RESULT"
  exit 1
fi
```

本机必须已有 Docker 和指定镜像；评测器不自动拉取镜像，也不退回宿主机执行。
镜像在 Agent 开始前解析成不可变 ID；测试脚本也在这时快照并计算哈希。
Agent 和受管进程关闭后，每个验收器拿到独立的最终产物副本，互相不能污染。

容器断网、根文件系统只读、移除 capabilities、禁止提权，限制为 1 CPU、1 GiB 内存、128 个进程和 128 MiB `/tmp`。
`/verifier` 只读，`/workspace` 是可写的独立副本，`/verifier-output` 存放结果。
不会将宿主机模型密钥传入验收容器。模型生成的代码如果需要运行，也只在该容器中由受信任测试调用。

验收器必须向 `$BOT_EVAL_RESULT` 写入不超过 64 KiB 的普通 JSON 文件，包含真正的布尔 `passed`，以及可选 `message`。
通过要求 `passed: true` 且退出码 0。明确的 `passed: false` 表示任务失败；
缺结果、格式错误、超时、启动失败、声称通过但非零退出，以及容器清理失败都属于验收错误。
标准输出/错误最多保留 256 KiB，避免日志无限增长。

## 结果与复现记录

每次执行输出到 `--artifacts-dir/<attempt_id>/`（默认当前工作区 `.bot/evals`）：

- `result.json`：逐项验收、运行状态、失败类别及耗时、Token、费用等指标。
- `manifest.json`：Case、配置、源码版本/哈希、fixture 与最终文件哈希、变化列表、种子哈希、验收器哈希及镜像 ID。
- `events.jsonl`：本次捕获的执行轨迹，包含实际工具结果。
- `artifacts/`：本次新增或改变的普通文件。删除和链接只在清单中记录；包含已知模型密钥的文件不导出，清单列出省略项。

配置不包含 API Key；轨迹和报告经过现有脱敏器。输入快照、最终快照的哈希针对原始内容，不能用脱敏后的文件反算它们。
快照清单用于确认输入和版本是否一致，不包含全量输入归档；复现仍需要保留 fixture、种子、Skill、配置和镜像。

结果格式为 `schema_version: 2`：

| 字段 | 解释 |
|---|---|
| `run_status` | Agent 状态，环境准备失败时为 `not_started`；兼容字段 `status` 与它相同 |
| `verdict` | `pass`：全部条件通过；`fail`：条件不满足；`error`：无法可靠执行或验收 |
| `failure_kind` | `task`、`environment`、`provider`、`budget`、`verifier`、`runtime`；通过时为空 |
| `checks` | 每个条件自己的 `pass/fail/error`、原因和证据 |
| `passed` | 仅在 `verdict == pass` 时为真，保留给旧 JSONL 消费者 |

例如 Agent 报告 `completed`，但 JSON 数值错误时，是 `run_status: completed`、`verdict: fail`、`failure_kind: task`。
镜像不可用是 `not_started/error/environment`；验收超时是 `completed/error/verifier`。
不要把所有非通过样本直接合并为能力失败率，应同时报告环境、Provider 和验收错误的数量。

## 实现回归

```bash
.venv/bin/pytest -q tests/unit/test_evals.py tests/integration/test_eval_verification.py
RUN_EVAL_DOCKER_TESTS=1 .venv/bin/pytest -q tests/integration/test_eval_verification.py
```

第二条显式启用本地 `alpine:3.20` 的真实 Docker 验收测试；按本机配置设置 `DOCKER_CONTEXT`。
覆盖正确产物、旧文件误通过、请求与执行脱节、错误参数/退出码、子运行混淆、无效 JSON、禁止修改、共享状态、
私有验收脚本、脱敏、异常分类和资源清理。公开任务集接入、重复采样统计及能力基线属于后续阶段。

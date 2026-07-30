# Terminal-Bench 2.1 评测

本项目通过 Harbor 的自定义 `BaseInstalledAgent` 接口接入 Terminal-Bench 2.1。官方数据集
标识为 `terminal-bench/terminal-bench-2-1`，包含 89 个任务。

## 设计

宿主机脚本先把当前提交对应的源码构建为 wheel，再由 Harbor 为每个任务创建一次性容器。
`KunpengBot` 适配器把 wheel 和 Bot 配置上传到容器，在独立 Python 虚拟环境中安装，并把
任务 instruction 交给容器专用 worker。镜像已有 Python 3.12+ 时直接复用，否则由 `uv`
安装托管 Python 3.12。Harbor 在 Agent 结束后运行任务自带 verifier。

这条链路有几个刻意的边界：

- 不修改 Harbor 源码，适配器通过
  `bot.evals.harbor_agent:KunpengBot` import path 加载；
- 不把宿主项目目录可写挂载到任务容器，只上传构建产物；
- API Key 通过 Harbor 的 `${ENV_VAR}` 模板解析，命令行、job 配置和 Git 文件中都不保存
  明文；
- 默认关闭鲲鹏 Skill 和子 Agent，以建立核心 Agent loop 的基线；`--subagents` 可显式开启
  子 Agent；
- 容器内启用自动审批、网络 Tool 和工作区外路径，因为 Terminal-Bench 任务可能需要安装
  软件、修改 `/etc` 或操作系统服务；实际网络仍受任务和 Harbor 的容器策略约束；
- Agent 达到内部步数、费用或时间限制时，worker 仍以成功进程状态交还 Harbor，让 verifier
  对容器中的部分结果评分。

Harbor 固定为 `0.20.0`，避免自定义 Agent API 漂移影响重复实验。升级 Harbor 时应重新运行
适配器单测和 smoke task。

## 前置条件

- Docker Desktop 或兼容 Docker daemon 正常运行；
- 已安装 `uv`/`uvx`；
- 项目 `.venv` 已安装；
- `.bot/config.toml` 中的 `model.base_url` 是容器可访问的 HTTPS 地址；
- `model.api_key_ref` 指向的环境变量已经导出。

脚本通过 `uvx` 创建隔离的 Harbor 工具环境，不会把 Harbor 的依赖安装进项目 `.venv`。
首次运行需要下载 Harbor、Terminal-Bench task、容器镜像和 Agent 依赖；镜像缺少
Python 3.12+ 时还会下载托管 Python。

## 单任务 smoke test

先运行一个较小任务：

```bash
.venv/bin/python scripts/run_terminalbench.py \
  --task openssl-selfsigned-cert \
  --n-concurrent 1 \
  --max-steps 60 \
  --max-wall-time-seconds 1800 \
  --max-cost-usd 1.0
```

可以重复 `--task` 选择多个任务：

```bash
.venv/bin/python scripts/run_terminalbench.py \
  --task regex-log \
  --task cancel-async-tasks
```

只做预检、构建 wheel 并查看最终 Harbor 命令：

```bash
.venv/bin/python scripts/run_terminalbench.py \
  --task openssl-selfsigned-cert \
  --dry-run
```

如需传递脚本尚未封装的 Harbor 参数，放在 `--` 后面：

```bash
.venv/bin/python scripts/run_terminalbench.py \
  --task openssl-selfsigned-cert \
  -- --debug
```

## 全量运行

全量 89 个任务必须显式使用 `--all`，防止误触发高成本实验：

```bash
.venv/bin/python scripts/run_terminalbench.py \
  --all \
  --n-concurrent 4 \
  --n-attempts 1 \
  --max-cost-usd 2.0
```

正式对比时应固定以下条件并记录在实验说明中：

- 本项目 Git commit 和构建 wheel；
- Harbor 版本；
- 数据集版本；
- 模型、端点和推理参数；
- Agent 步数、时间、费用与子 Agent 开关；
- Docker 镜像与宿主 CPU 架构；
- task 重复次数和并发数。

Terminal-Bench leaderboard 要求的重复次数和公开上传规则可能变化，提交前应以官方仓库为准。
本地单次 smoke test 只验证接入链路，不构成可提交的 leaderboard 成绩。

## 结果与诊断

默认输出位于：

```text
artifacts/terminalbench/
├── packages/                 # 注入任务容器的项目 wheel
└── jobs/                     # Harbor job 和 trial 结果
    └── <job>/
        └── .../
            ├── agent/
            │   ├── instruction.md
            │   ├── events.jsonl
            │   ├── result.json
            │   ├── state.db
            │   ├── worker.log
            │   └── trace/
            ├── verifier/
            ├── config.json
            └── result.json
```

`agent/trace/transcript.md` 展示完整对话、推理和 Tool 调用；`events.jsonl` 可用于程序化分析；
Harbor 的 `result.json` 和 verifier 日志给出任务 reward。

## 本地链路验证

2026-07-30 在 macOS ARM64 + Docker Desktop（Linux/AMD64 task 镜像由 QEMU 模拟）上完成
`openssl-selfsigned-cert` smoke test：

- Harbor verifier reward：`1.0`；
- Agent 状态：`completed`，18 步，21 次 Tool Call；
- 模型用量：118,632 input tokens、5,203 output tokens、记录费用 `$0.129038`；
- 首次运行总耗时：3 分 38 秒，其中 Agent 执行 1 分 47 秒；
- Harbor 汇总无 exception，完整事件、SQLite 状态和 Trace Bundle 均成功回收；
- 对 job 目录扫描实际 API Key，明文命中数为 0。

这个结果只证明部署与判分链路可用，不代表 89 个任务的总体能力。

## 已知限制

- macOS ARM64 上的本地 Docker 适合接入 smoke test，但部分 Terminal-Bench 镜像或二进制任务
  依赖 x86_64。正式可比实验应使用与官方运行一致的 Linux/x86_64 环境或受支持的云 sandbox。
- 本项目安全策略即使在 `full-access` 下仍拒绝 `.env`、`.ssh` 等敏感路径和声明为
  `secret_access` 的 Tool。它保留了生产 Agent 的安全边界，可能使少数合法取证类 benchmark
  任务低估能力；如要研究“完全无约束容器 Agent”，应实现独立 benchmark policy，而不是
  放宽普通运行时默认策略。
- 安装 Agent 需要任务环境在 setup 阶段能访问 Astral 和 Python package registry。若某个
  task 明确禁网，应该预构建包含 Python 与依赖的 Agent 镜像；不要通过放宽 verifier 网络来
  改变 benchmark 语义。

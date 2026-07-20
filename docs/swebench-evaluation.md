# SWE-bench 评测

本项目通过一个薄适配层参与 SWE-bench：适配层准备指定的仓库基线，把公开 issue
题面交给 `bot`，收集工作区 Git diff，并输出官方 Harness 接受的 prediction JSONL。

## 前置条件

- Docker 正常运行；
- 独立环境中安装官方 `swebench`；
- 项目 `.venv` 已安装；
- 配置引用的模型 API Key 已注入环境变量。

建议让 SWE-bench 和本项目使用不同虚拟环境，避免评测依赖影响项目运行依赖。

## 单实例流程

先从 SWE-bench 数据集导出一个公开 instance，内容至少包含：

```json
{
  "instance_id": "sympy__sympy-20590",
  "repo": "sympy/sympy",
  "base_commit": "cffd4e0f86fefd4802349a9f9b19ed70934ea354",
  "problem_statement": "..."
}
```

推荐在官方 instance 镜像创建的一次性容器中运行 Agent。这样可以自动批准 Python、
构建器和测试命令，而不会把无条件批准策略带到宿主机：

```bash
.venv/bin/python scripts/run_swebench_container.py instance.json \
  --image sweb.eval.x86_64.sympy__sympy-20590:latest \
  --project-root "$PWD" \
  --config .bot/config.toml \
  --output artifacts/swebench/predictions.jsonl
```

容器适配器会：

1. 使用给定 SWE-bench instance 镜像启动一次性 Linux/AMD64 容器；
2. 把项目源码只读挂载到容器，另建 Python 3.12 环境安装 Agent；
3. 仅在容器内启用自动批准，并把 Agent 工作区限定为 `/testbed`；
4. 收集 binary-safe Git diff，排除 `.bot/` 运行状态；
5. 无论成功或失败都删除该次创建的容器。

模型 HTTPS 请求通过评测期间临时启动的受限 CONNECT 出口转发。该出口使用随机端口、
只允许连接 `model.base_url` 对应的主机和端口，并在运行结束后关闭。这能避开本地代理
对特定模型域名的 TLS 兼容问题，又不会开放通用代理。

也可以直接在宿主机的一次性 Git 工作区运行 Agent：

```bash
.venv/bin/python scripts/run_swebench_instance.py instance.json \
  --workspace /tmp/swebench-work/sympy__sympy-20590 \
  --config .bot/config.toml \
  --output artifacts/swebench/predictions.jsonl
```

宿主机适配器拒绝覆盖已存在的工作区。它会：

1. 只 fetch `base_commit` 并 detached checkout；
2. 把 `problem_statement` 交给非交互 `bot run`；
3. 收集修改文件和新增文件的 binary-safe diff；
4. 排除 `.bot/` 运行状态；
5. 写出 `instance_id`、`model_name_or_path` 和 `model_patch`。

最后使用官方 Harness 判分：

```bash
python -m swebench.harness.run_evaluation \
  --dataset_name SWE-bench/SWE-bench_Lite \
  --predictions_path artifacts/swebench/predictions.jsonl \
  --instance_ids sympy__sympy-20590 \
  --max_workers 1 \
  --run_id kunpeng-cli-agent-smoke
```

在 macOS ARM64 上需要追加 `--namespace ''`，由本机从头构建 Linux/AMD64
评测镜像；首次构建会明显慢于后续运行。

## 结果文件

- `predictions.jsonl`：提交给官方 Harness 的补丁；
- `predictions.events.jsonl`：本项目 JSONL 事件轨迹；
- `predictions.stderr.log`：Agent 进程诊断信息；
- `predictions.setup.log`：容器创建、依赖安装和销毁日志（容器模式）；
- SWE-bench `evaluation_results/`：官方 resolved/unresolved 判分。

不得把 instance 的 `patch`、`test_patch`、`FAIL_TO_PASS` 或 `PASS_TO_PASS`
交给 Agent；这些字段只能由评测端使用。

## 代理与执行隔离

如果本地 HTTP/SOCKS 代理对模型域名的 TLS 转发失败，而模型端点可以直连，可仅为
模型域名设置 `NO_PROXY`，例如：

```bash
NO_PROXY=api.deepseek.com no_proxy=api.deepseek.com \
  .venv/bin/python scripts/run_swebench_instance.py ...
```

宿主机模式沿用项目的非交互安全策略。需要自动批准 Python、构建器或任意测试命令时，
应使用容器模式，不要在宿主机评测中直接使用无条件批准策略。容器 Worker 还会检查
`SWEBENCH_CONTAINER=1`、`/.dockerenv` 和精确的 `/testbed` 工作区，避免被误用于宿主机。

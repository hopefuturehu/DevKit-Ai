# MVP 实现状态

> 更新日期：2026-07-17
> 结论：设计中 Milestone 0–3 的本地代码闭环已经实现；需要真实凭据或鲲鹏 ARM 环境的项目保留为环境验收，不伪造通过结果。

## 已完成

| 设计域 | 实现结果 | 主要入口 |
|---|---|---|
| 通用 CLI | 交互、自然语言入口、单次运行、JSONL、管理命令、会话内命令、steering 与取消 | `bot`、`bot run`、`bot resume` |
| Provider | OpenAI-compatible Chat Completions，SSE 文本、结构化 Tool Call、usage 与异常归一化 | `src/bot/providers/` |
| Agent Core | 多轮 Tool Loop、预算、费用、上下文上限、重复失败和幂等无进展熔断 | `src/bot/core/agent.py` |
| 通用 Tool | read、search、精确 patch、argv command、受控 shell、HTTP fetch | `src/bot/tools/builtins.py` |
| 执行抽象 | `ExecutionTarget` 接口、本地异步 subprocess、流式输出、超时与进程组终止 | `src/bot/execution/` |
| 安全策略 | workspace/symlink 边界、敏感路径、危险命令审批、非 TTY fail-closed、环境变量 allowlist、脱敏 | `src/bot/policy/`、`src/bot/observability/` |
| 审批 | once/session/always/deny；永久授权按 workspace、Tool 和精确结构化参数匹配 | `src/bot/core/approval.py` |
| 会话与审计 | SQLite migration v4、版本化事件、消息/Tool/审批投影、resume/fork、显式记忆和用量统计 | `src/bot/sessions/` |
| 上下文 | Core Policy、`AGENTS.md`、环境、Skill Catalog、显式记忆、历史、确定性压缩和 manifest | `src/bot/core/context.py` |
| Skill | 单一目录发现、资格过滤、三段式披露、显式/自动多选、资源按需加载和 reload | `src/bot/skills/` |
| 鲲鹏扩展 | KSYS、DevKit Tuner 结构化 Subprocess Adapter；非 ARM/缺工具时返回手动命令 | `src/bot/tools/kunpeng/` |
| 领域手册 | 可偏离的 `kunpeng-performance-analysis` Skill，随 wheel 发布并由 `bot init` 安装 | `skills/kunpeng-performance-analysis/` |
| 评测 | JSONL Case、结果/文件/Tool/Skill/审批断言、Token/费用指标、关闭 Skill 的对照组 | `bot eval run`、`evals/` |

## 验证证据

```bash
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/pytest -q
.venv/bin/pip check
.venv/bin/pip wheel . --no-deps --no-build-isolation --wheel-dir /tmp/bot-wheels
```

当前确定性测试共 42 项，覆盖 Provider 流、完整 Agent Loop、审批作用域、上下文压缩、会话迁移和恢复、Skill 多选、路径逃逸、进程组终止、KSYS/Tuner 参数映射、CLI JSONL 以及 Eval runner。构建后的 wheel 另行执行了 `bot init` 烟测，并确认内置 Skill 可释放到工作区。

## 仍需环境验收

以下项目依赖当前工作区没有提供的凭据、软件或硬件，不属于用 mock 冒充通过的项目：

- 使用实际 DeepSeek V4 Flash/Pro 模型 ID，验证目标 OpenAI-compatible 服务的流式 Tool Calling、usage、finish reason、延迟和费用；
- 在安装 KSYS 的 Linux 环境验证 collect/report/diff/stability-check 的实际 CLI 版本和输出；
- 在鲲鹏 ARM 主机验证 DevKit Tuner 的各任务、权限、采集开销和输出解析；
- 运行 `evals/generic.jsonl` 与 `evals/kunpeng.jsonl` 的多轮重复实验，形成带/不带 Skill 的统计对比；
- 在客户部署边界内验证 API 地址、日志保留、代理设置和数据出境策略。

## 按设计延期

SSH ExecutionTarget、MCP/Python/HTTP 通用 Adapter、私有数据库/RAG、Skill 安装签名和多来源、Gateway、后台任务、多 Agent、专用跨机器结果包均保持接口或演进空间，但没有纳入本次 MVP。当前密钥实现只接受环境变量引用；操作系统 Keychain 可在后续安全增强中补充。

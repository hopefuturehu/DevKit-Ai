# bot

`bot` 是一个通用的本地优先 CLI Agent。核心通过 OpenAI-compatible API 调用模型，并以
Skill 和 Tool 扩展鲲鹏迁移、性能分析等领域能力。

当前实现目标和边界见 [docs/design.md](docs/design.md)。

## 开发安装

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

## 最小配置

在工作区创建 `.bot/config.toml`：

```toml
[model]
provider = "openai_compatible"
base_url = "https://your-provider.example/v1"
api_key_ref = "env:BOT_MODEL_API_KEY"
name = "your-model-id"

[skills]
path = "./skills"
```

然后运行：

```bash
export BOT_MODEL_API_KEY='...'
bot doctor
bot
```

也可以执行单次任务：

```bash
bot run "分析这个项目"
bot run --json "列出当前项目结构"
```


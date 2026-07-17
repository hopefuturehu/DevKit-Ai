from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from typer.core import TyperGroup

from bot import __version__
from bot.cli.render import InteractiveApprovalHandler, RichEventSink
from bot.cli.runtime import build_runtime
from bot.config import ConfigError, load_config, resolve_api_key
from bot.core.events import JsonlEventSink
from bot.core.models import RunRequest
from bot.execution import LocalExecutionTarget
from bot.providers import ProviderError
from bot.sessions import SQLiteSessionStore
from bot.skills import SkillCatalog


class NaturalLanguageGroup(TyperGroup):
    """Route an unknown first token to the hidden natural-language command."""

    def resolve_command(self, ctx, args):
        if args and args[0] not in self.commands and not args[0].startswith("-"):
            args.insert(0, "__chat")
        return super().resolve_command(ctx, args)


app = typer.Typer(
    name="bot",
    help="通用、可扩展的本地优先 CLI Agent。",
    no_args_is_help=False,
    invoke_without_command=True,
    cls=NaturalLanguageGroup,
)
skill_app = typer.Typer(help="查看和诊断 Skill。")
session_app = typer.Typer(help="查看和管理会话。")
config_app = typer.Typer(help="查看生效配置。")
app.add_typer(skill_app, name="skill")
app.add_typer(session_app, name="session")
app.add_typer(config_app, name="config")
console = Console()
DEFAULT_WORKSPACE = Path.cwd()


def _run(coroutine):
    try:
        return asyncio.run(coroutine)
    except KeyboardInterrupt:
        console.print("\n[yellow]已中断。[/yellow]")
        raise typer.Exit(130) from None


def _parse_prompt(prompt: str) -> tuple[str, list[str]]:
    tokens = prompt.split()
    skills: list[str] = []
    while tokens and re.fullmatch(r"\$[a-z0-9_-]+", tokens[0]):
        skills.append(tokens.pop(0)[1:])
    return " ".join(tokens).strip(), skills


def _runtime(
    workspace: Path,
    config_path: Path | None,
    *,
    json_output: bool,
    interactive: bool,
):
    sinks = [JsonlEventSink()] if json_output else [RichEventSink(console)]
    approval = InteractiveApprovalHandler(console) if interactive else None
    try:
        return build_runtime(
            workspace=workspace,
            config_path=config_path,
            event_sinks=sinks,
            approval_handler=approval,
        )
    except (ConfigError, ProviderError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc


@app.callback()
def main(
    ctx: typer.Context,
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help="工作区目录")
    ] = DEFAULT_WORKSPACE,
    config_path: Annotated[Path | None, typer.Option("--config", help="显式配置文件")] = None,
    version: Annotated[bool, typer.Option("--version", help="显示版本")] = False,
) -> None:
    if version:
        console.print(__version__)
        raise typer.Exit()
    if ctx.invoked_subcommand is not None:
        ctx.ensure_object(dict)
        ctx.obj.update({"workspace": workspace, "config_path": config_path})
        return
    runtime = _runtime(workspace, config_path, json_output=False, interactive=True)
    try:
        session_id = runtime.store.create_session(workspace)
        _interactive_loop(runtime, session_id)
    finally:
        runtime.close()


@app.command("__chat", hidden=True)
def chat_command(
    ctx: typer.Context,
    prompt: Annotated[str, typer.Argument(help="初始任务")],
) -> None:
    workspace = ctx.obj["workspace"]
    config_path = ctx.obj["config_path"]
    runtime = _runtime(workspace, config_path, json_output=False, interactive=True)
    try:
        session_id = runtime.store.create_session(workspace)
        clean_prompt, explicit_skills = _parse_prompt(prompt)
        result = _run(
            runtime.runner.run(
                RunRequest(
                    prompt=clean_prompt,
                    session_id=session_id,
                    explicit_skills=explicit_skills,
                )
            )
        )
        if result.status not in {"completed"}:
            console.print(f"[red]{result.error or result.status}[/red]")
        _interactive_loop(runtime, session_id)
    finally:
        runtime.close()


def _interactive_loop(runtime, session_id: str) -> None:
    console.print(
        f"[bold]bot[/bold] {__version__} · session {session_id[:8]} · "
        f"{runtime.config.model.name or '<model-unset>'}"
    )
    while True:
        try:
            prompt = console.input("[bold cyan]> [/bold cyan]").strip()
        except EOFError:
            console.print()
            return
        if not prompt:
            continue
        if prompt in {"/exit", "/quit"}:
            return
        if prompt == "/status":
            console.print(
                {
                    "session": session_id,
                    "workspace": str(runtime.workspace),
                    "model": runtime.config.model.name,
                    "base_url": runtime.config.model.base_url,
                    "permission_mode": runtime.config.permissions.mode,
                    "active_skills": list(runtime.skills.active),
                }
            )
            continue
        if prompt == "/tools":
            console.print("\n".join(runtime.tools.names()))
            continue
        if prompt == "/skills":
            _print_skills(runtime.catalog, active=set(runtime.skills.active))
            continue
        if prompt == "/skills reload":
            runtime.catalog.scan()
            runtime.skills.reset()
            console.print(
                f"已重新扫描 {runtime.catalog.root}，可用 {len(runtime.catalog.skills)} 个 Skill。"
            )
            continue
        if prompt in {"/help", "?"}:
            console.print("/status /tools /skills /skills reload /new /exit")
            continue
        if prompt == "/new":
            session_id = runtime.store.create_session(runtime.workspace)
            runtime.skills.reset()
            console.print(f"已创建会话 {session_id[:8]}")
            continue
        clean_prompt, explicit_skills = _parse_prompt(prompt)
        if not clean_prompt:
            console.print("[yellow]请输入 Skill 后的任务内容。[/yellow]")
            continue
        result = _run(
            runtime.runner.run(
                RunRequest(
                    prompt=clean_prompt,
                    session_id=session_id,
                    explicit_skills=explicit_skills,
                )
            )
        )
        if result.status not in {"completed"}:
            console.print(f"[red]{result.error or result.status}[/red]")


@app.command("run")
def run_command(
    ctx: typer.Context,
    prompt: Annotated[str, typer.Argument(help="要执行的任务")],
    json_output: Annotated[bool, typer.Option("--json", help="输出 JSONL 事件")] = False,
) -> None:
    workspace = ctx.obj["workspace"]
    config_path = ctx.obj["config_path"]
    runtime = _runtime(workspace, config_path, json_output=json_output, interactive=not json_output)
    try:
        clean_prompt, explicit_skills = _parse_prompt(prompt)
        result = _run(
            runtime.runner.run(
                RunRequest(
                    prompt=clean_prompt,
                    explicit_skills=explicit_skills,
                    json_output=json_output,
                )
            )
        )
        if result.status != "completed":
            if json_output:
                print(result.model_dump_json())
            else:
                console.print(f"[red]{result.error or result.status}[/red]")
            raise typer.Exit(1)
    finally:
        runtime.close()


@app.command("resume")
def resume_command(
    ctx: typer.Context,
    session_id: Annotated[str | None, typer.Argument(help="会话 ID")] = None,
) -> None:
    workspace = ctx.obj["workspace"]
    config_path = ctx.obj["config_path"]
    runtime = _runtime(workspace, config_path, json_output=False, interactive=True)
    try:
        selected = session_id or runtime.store.latest_session(workspace)
        if not selected or not runtime.store.session_exists(selected):
            console.print("[red]没有可恢复的会话。[/red]")
            raise typer.Exit(1)
        _interactive_loop(runtime, selected)
    finally:
        runtime.close()


@app.command("doctor")
def doctor_command(ctx: typer.Context) -> None:
    workspace = ctx.obj["workspace"].resolve()
    config_path = ctx.obj["config_path"]
    errors = 0
    try:
        config = load_config(workspace, config_path=config_path)
        console.print("[green]✓[/green] 配置 Schema 有效")
    except ConfigError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(2) from exc
    if config.model.base_url:
        console.print(f"[green]✓[/green] Model base_url: {config.model.base_url}")
    else:
        errors += 1
        console.print("[red]✗[/red] model.base_url 未配置")
    if config.model.name:
        console.print(f"[green]✓[/green] Model: {config.model.name}")
    else:
        errors += 1
        console.print("[red]✗[/red] model.name 未配置")
    try:
        resolve_api_key(config.model.api_key_ref)
        console.print(f"[green]✓[/green] API Key 引用有效: {config.model.api_key_ref}")
    except ConfigError as exc:
        errors += 1
        console.print(f"[red]✗[/red] {exc}")

    target = LocalExecutionTarget()
    environment = _run(target.probe(["ksys", "devkit", "rg", "git"]))
    console.print(
        f"[green]✓[/green] 环境: {environment.operating_system}/{environment.architecture}"
    )
    for name, path in environment.executables.items():
        marker = "[green]✓[/green]" if path else "[yellow]-[/yellow]"
        console.print(f"{marker} {name}: {path or '未安装'}")

    catalog = SkillCatalog(config.skill_path(workspace))
    catalog.scan()
    console.print(f"[green]✓[/green] Skill 可用: {len(catalog.skills)}")
    for diagnostic in catalog.diagnostics:
        color = "red" if diagnostic.level == "error" else "yellow"
        console.print(
            f"[{color}]{diagnostic.level}[/{color}] {diagnostic.path}: {diagnostic.message}"
        )
        if diagnostic.level == "error":
            errors += 1
    try:
        store = SQLiteSessionStore(config.state_path())
        store.close()
        console.print(f"[green]✓[/green] 状态库可写: {config.state_path()}")
    except OSError as exc:
        errors += 1
        console.print(f"[red]✗[/red] 状态库不可写: {exc}")
    if errors:
        raise typer.Exit(1)


@app.command("init")
def init_command(ctx: typer.Context) -> None:
    workspace = ctx.obj["workspace"].resolve()
    target = workspace / ".bot" / "config.toml"
    if target.exists():
        console.print(f"[yellow]配置已存在：{target}[/yellow]")
        raise typer.Exit(1)
    target.parent.mkdir(parents=True, exist_ok=True)
    template = """[model]
provider = "openai_compatible"
base_url = ""
api_key_ref = "env:BOT_MODEL_API_KEY"
name = ""

[skills]
path = "./skills"
auto_activate = true
max_auto_activated = 3
"""
    target.write_text(template, encoding="utf-8")
    (workspace / "skills").mkdir(exist_ok=True)
    console.print(f"[green]已创建[/green] {target}")


@skill_app.command("list")
def skill_list(ctx: typer.Context) -> None:
    workspace = ctx.obj["workspace"].resolve()
    config = load_config(workspace, config_path=ctx.obj["config_path"])
    catalog = SkillCatalog(config.skill_path(workspace))
    catalog.scan()
    _print_skills(catalog)


@skill_app.command("show")
def skill_show(ctx: typer.Context, name: str) -> None:
    workspace = ctx.obj["workspace"].resolve()
    config = load_config(workspace, config_path=ctx.obj["config_path"])
    catalog = SkillCatalog(config.skill_path(workspace))
    catalog.scan()
    skill = catalog.get(name)
    if not skill:
        console.print(f"[red]Skill 不存在或不可用：{name}[/red]")
        raise typer.Exit(1)
    console.print(f"[bold]{skill.name}[/bold]\n{skill.description}\n[dim]{skill.path}[/dim]\n")
    console.print(skill.instructions, markup=False)


def _print_skills(catalog: SkillCatalog, active: set[str] | None = None) -> None:
    table = Table("名称", "状态", "说明", "路径")
    active = active or set()
    for skill in catalog.skills.values():
        table.add_row(
            skill.name,
            "active" if skill.name in active else "available",
            skill.description,
            str(skill.path),
        )
    console.print(table)
    for diagnostic in catalog.diagnostics:
        console.print(f"[{diagnostic.level}] {diagnostic.path}: {diagnostic.message}")


@session_app.command("list")
def session_list(ctx: typer.Context) -> None:
    workspace = ctx.obj["workspace"].resolve()
    config = load_config(workspace, config_path=ctx.obj["config_path"])
    store = SQLiteSessionStore(config.state_path())
    try:
        table = Table("Session", "Workspace", "Updated")
        for item in store.list_sessions():
            table.add_row(item["id"], item["workspace"], item["updated_at"])
        console.print(table)
    finally:
        store.close()


@config_app.command("get")
def config_get(ctx: typer.Context) -> None:
    workspace = ctx.obj["workspace"].resolve()
    config = load_config(workspace, config_path=ctx.obj["config_path"])
    data = config.model_dump(mode="json")
    data["model"]["api_key_ref"] = config.model.api_key_ref
    console.print_json(json.dumps(data, ensure_ascii=False))


if __name__ == "__main__":
    app()

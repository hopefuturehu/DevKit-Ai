from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import signal
import sqlite3
import stat
import subprocess
import sys
from importlib.resources import files
from pathlib import Path
from typing import Annotated

import typer
from dotenv import dotenv_values
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from rich.table import Table
from typer.core import TyperGroup

from bot import __version__
from bot.cli.render import InteractiveApprovalHandler, RichEventSink, create_cli_console
from bot.cli.runtime import build_runtime
from bot.config import (
    ConfigError,
    api_key_reference_variable,
    config_target,
    get_config_value,
    load_config,
    parse_config_value,
    resolve_model_api_key,
    set_config_value,
)
from bot.core.events import JsonlEventSink
from bot.core.models import RunRequest
from bot.evals import load_eval_cases, run_eval_case, write_eval_results
from bot.execution import LocalExecutionTarget
from bot.memory import MarkdownMemoryStore
from bot.observability import export_trace_bundle
from bot.providers import ProviderError
from bot.sessions import SQLiteSessionStore
from bot.skills import SkillCatalog
from bot.subagents import AgentCatalog
from bot.tools import ToolRegistry, register_builtin_tools
from bot.tools.kunpeng import register_kunpeng_tools


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
agent_app = typer.Typer(help="查看、校验和信任 Markdown Agent。")
session_app = typer.Typer(help="查看和管理会话。")
config_app = typer.Typer(help="查看生效配置。")
model_app = typer.Typer(help="查看或切换模型。")
eval_app = typer.Typer(help="运行可复现的 Agent 评测任务。")
trace_app = typer.Typer(help="导出和阅读完整 Agent 执行轨迹。")
web_app = typer.Typer(help="启动 Web 界面。")
app.add_typer(skill_app, name="skill")
app.add_typer(agent_app, name="agent")
app.add_typer(session_app, name="session")
app.add_typer(config_app, name="config")
app.add_typer(model_app, name="model")
app.add_typer(eval_app, name="eval")
app.add_typer(trace_app, name="trace")
app.add_typer(web_app, name="web")
console = create_cli_console()
DEFAULT_WORKSPACE = Path.cwd()


def _copy_resource_tree(source, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        target = destination / child.name
        if child.is_dir():
            _copy_resource_tree(child, target)
        elif not target.exists():
            target.write_bytes(child.read_bytes())


def _install_bundled_skills(destination: Path) -> list[str]:
    packaged = files("bot").joinpath("assets", "skills")
    source = packaged if packaged.is_dir() else Path(__file__).resolve().parents[3] / "skills"
    if not source.is_dir():
        return []
    installed: list[str] = []
    destination.mkdir(parents=True, exist_ok=True)
    for skill in sorted(source.iterdir(), key=lambda item: item.name):
        if not skill.is_dir():
            continue
        target = destination / skill.name
        if target.exists():
            continue
        _copy_resource_tree(skill, target)
        installed.append(skill.name)
    return installed


def _run(coroutine):
    try:
        return asyncio.run(coroutine)
    except KeyboardInterrupt:
        console.print("\n[yellow]已中断。[/yellow]")
        raise typer.Exit(130) from None


async def _with_runtime_shutdown(runtime, coroutine):
    loop = asyncio.get_running_loop()
    current_task = asyncio.current_task()
    received_signal: int | None = None
    shutting_down = False
    installed_handlers: dict[int, object] = {}

    def request_shutdown(signum: int) -> None:
        nonlocal received_signal
        if shutting_down or received_signal is not None or current_task is None:
            return
        received_signal = signum
        current_task.cancel()

    for candidate in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None)):
        if not isinstance(candidate, int):
            continue
        try:
            previous = signal.getsignal(candidate)
            loop.add_signal_handler(candidate, request_shutdown, candidate)
        except (NotImplementedError, RuntimeError, ValueError):
            continue
        installed_handlers[candidate] = previous
    try:
        return await coroutine
    except asyncio.CancelledError:
        if received_signal is not None:
            raise typer.Exit(128 + received_signal) from None
        raise
    finally:
        shutting_down = True
        cleanup = asyncio.create_task(runtime.aclose(), name="runtime-shutdown")
        try:
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            cleanup.result()
        finally:
            for signum, previous in installed_handlers.items():
                loop.remove_signal_handler(signum)
                try:
                    signal.signal(signum, previous)
                except (OSError, RuntimeError, ValueError):
                    pass


async def _prompt_with_background_approvals(
    runtime,
    prompt_session: PromptSession[str],
) -> str | None:
    approval_handler = runtime.approval_handler
    if not isinstance(approval_handler, InteractiveApprovalHandler):
        return await prompt_session.prompt_async("> ")
    input_task = asyncio.create_task(prompt_session.prompt_async("> "))
    approval_task = asyncio.create_task(approval_handler.next_request())
    try:
        with patch_stdout():
            done, _ = await asyncio.wait(
                {input_task, approval_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        if approval_task in done:
            input_completed = input_task in done
            if not input_task.done():
                input_task.cancel()
                await asyncio.gather(input_task, return_exceptions=True)
            with patch_stdout():
                await approval_handler.resolve(approval_task.result(), prompt_session)
            return input_task.result() if input_completed else None
        approval_task.cancel()
        await asyncio.gather(approval_task, return_exceptions=True)
        return input_task.result()
    finally:
        for task in (input_task, approval_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(input_task, approval_task, return_exceptions=True)


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
    try:
        preview_config = load_config(workspace.resolve(), config_path=config_path)
        sinks = (
            [JsonlEventSink()]
            if json_output
            else [
                RichEventSink(
                    console,
                    show_tool_output=preview_config.display.tool_output == "full",
                )
            ]
        )
        approval = InteractiveApprovalHandler(console) if interactive else None
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
        _run(_with_runtime_shutdown(runtime, _interactive_loop(runtime, session_id)))
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
        _run(
            _with_runtime_shutdown(
                runtime,
                _interactive_loop(runtime, session_id, initial_prompt=prompt),
            )
        )
    finally:
        runtime.close()


async def _interactive_loop(runtime, session_id: str, initial_prompt: str | None = None) -> None:
    if runtime.config.subagents.enabled:
        await runtime.subagents.start()
    prompt_session: PromptSession[str] = PromptSession()
    queued_prompt = initial_prompt
    console.print(
        f"[bold]bot[/bold] {__version__} · session {session_id[:8]} · "
        f"{runtime.config.model.name or '<model-unset>'}"
    )
    while True:
        try:
            if queued_prompt is not None:
                prompt = queued_prompt.strip()
                queued_prompt = None
            else:
                pending_input = await _prompt_with_background_approvals(runtime, prompt_session)
                if pending_input is None:
                    continue
                prompt = pending_input.strip()
        except EOFError:
            console.print()
            return
        if not prompt:
            continue
        if prompt in {"/exit", "/quit"}:
            return
        if prompt == "/status":
            usage = runtime.store.session_usage(session_id)
            console.print(
                {
                    "session": session_id,
                    "workspace": str(runtime.workspace),
                    "model": runtime.config.model.name,
                    "base_url": runtime.config.model.base_url,
                    "permission_mode": runtime.config.permissions.mode,
                    "auto_approve": runtime.config.permissions.auto_approve,
                    "active_skills": runtime.runner.active_skill_names(session_id),
                    "context_manifest": runtime.context.manifest(),
                    "context": runtime.runner.context_status(session_id),
                    "memory": (
                        runtime.memory_store.stats()
                        if runtime.memory_store is not None
                        else {"legacy_sqlite": len(runtime.store.list_memories())}
                    ),
                    "usage": usage,
                }
            )
            continue
        if prompt == "/tools":
            control_tools = (
                [definition.name for definition in runtime.subagents.definitions()]
                if runtime.config.subagents.enabled
                else []
            )
            console.print("\n".join([*runtime.tools.names(), *control_tools]))
            continue
        if prompt == "/todo":
            _print_plan(runtime.store.load_plan(session_id))
            continue
        if prompt in {"/agents", "/agents tasks"}:
            tasks = runtime.subagents.list_tasks(session_id)
            if not tasks:
                console.print("暂无后台子 Agent 任务。")
            else:
                table = Table("Task", "Agent", "Status", "Required", "Objective")
                for task in tasks:
                    table.add_row(
                        str(task["id"]),
                        str(task["agent_name"]),
                        str(task["status"]),
                        str(task["required"]),
                        str(task["objective"])[:80],
                    )
                console.print(table)
            continue
        if prompt == "/agents list":
            _print_agents(runtime.agent_catalog)
            continue
        if prompt == "/agents reload":
            _reload_runtime_agents(runtime)
            console.print(f"已重新加载 {len(runtime.agent_catalog.agents)} 个 Agent。")
            _print_agent_diagnostics(runtime.agent_catalog)
            continue
        if prompt == "/agents trust":
            digest = AgentCatalog.compute_project_digest(
                runtime.config.project_agent_path(runtime.workspace)
            )
            runtime.store.trust_agent_workspace(runtime.workspace, digest)
            _reload_runtime_agents(runtime)
            console.print("已信任当前内容摘要对应的项目 Agent；文件变化后需重新信任。")
            continue
        if prompt == "/agents untrust":
            runtime.store.untrust_agent_workspace(runtime.workspace)
            _reload_runtime_agents(runtime)
            console.print("已取消当前工作区的项目 Agent 信任。")
            continue
        if prompt == "/model":
            console.print(f"{runtime.config.model.name} @ {runtime.config.model.base_url}")
            continue
        if prompt.startswith("/model "):
            runtime.config.model.name = prompt.removeprefix("/model ").strip()
            console.print(f"本会话模型已切换为 {runtime.config.model.name}")
            continue
        if prompt == "/permissions":
            console.print(
                {
                    "mode": runtime.config.permissions.mode,
                    "auto_approve": runtime.config.permissions.auto_approve,
                    "workspace_only": runtime.config.permissions.workspace_only,
                    "network": runtime.config.permissions.network,
                }
            )
            continue
        if prompt.startswith("/permissions "):
            mode = prompt.removeprefix("/permissions ").strip()
            if mode not in {"safe", "read-only", "full-access"}:
                console.print("[red]权限模式必须是 safe/read-only/full-access。[/red]")
                continue
            runtime.config.permissions.mode = mode
            console.print(f"本会话权限模式已切换为 {mode}")
            continue
        if prompt == "/compact":
            result = await runtime.runner.compact_session(session_id)
            if result["compacted"]:
                stop = f"，stop={result['reason']}" if result.get("reason") else ""
                console.print(
                    "已发布可恢复上下文摘要："
                    f"id={result['compaction_id']}，"
                    f"cursor={result['cursor_position']}，"
                    f"messages={result['messages_consolidated']}，"
                    f"summary_tokens≈{result['summary_tokens']}，"
                    f"requests={result['request_count']}，"
                    f"duration={float(result['duration_ms']) / 1000:.1f}s"
                    f"{stop}。"
                )
            else:
                console.print(f"无需压缩：{result['reason']}。")
            continue
        if prompt == "/compact rebuild":
            result = await runtime.compactor.rebuild(session_id)
            if result.compacted:
                console.print(
                    "已从原始 Transcript 重建摘要："
                    f"id={result.compaction_id}，"
                    f"cursor={result.covered_end_position}，"
                    f"summary_tokens≈{result.summary_tokens}。"
                )
            elif result.error:
                console.print(f"摘要重建失败，活动版本未变化：{result.error}")
            else:
                console.print(f"无法重建：{result.reason}")
            continue
        if prompt.startswith("/compact rollback "):
            compaction_id = prompt.removeprefix("/compact rollback ").strip()
            try:
                record = runtime.compactor.rollback(session_id, compaction_id)
            except ValueError as exc:
                console.print(f"摘要回滚失败：{exc}")
            else:
                console.print(
                    f"已回滚活动摘要：id={record['id']}，cursor={record['covered_end_position']}。"
                )
            continue
        if prompt == "/skills":
            _print_skills(
                runtime.catalog, active=set(runtime.runner.active_skill_names(session_id))
            )
            continue
        if prompt == "/skills reload":
            runtime.catalog.scan()
            console.print(
                f"已重新扫描 {runtime.catalog.root}，可用 {len(runtime.catalog.skills)} 个 Skill。"
            )
            continue
        if prompt in {"/help", "?"}:
            console.print(
                "/status /tools /todo /skills /skills reload /remember <text> "
                "/memories /forget <id-or-key> /memory extract [run-id] "
                "/agents tasks|list|reload|trust|untrust /model /permissions "
                "/compact /compact rebuild "
                "/compact rollback <id> /new /exit"
            )
            continue
        if prompt.startswith("/remember "):
            if runtime.memory_store is not None:
                memory_id = runtime.memory_store.add_user_memory(prompt.removeprefix("/remember "))
            else:
                memory_id = str(runtime.store.add_memory(prompt.removeprefix("/remember ")))
            console.print(f"已保存显式记忆 [{memory_id}]")
            continue
        if prompt == "/memories":
            memories = (
                runtime.memory_store.list_memories()
                if runtime.memory_store is not None
                else runtime.store.list_memories()
            )
            if not memories:
                console.print("暂无长期记忆。")
            for item in memories:
                if runtime.memory_store is not None:
                    console.print(
                        f"[{item.id}] {item.content} "
                        f"[dim]({item.origin}/{item.status.value}; key={item.key})[/dim]"
                    )
                else:
                    console.print(f"[{item['id']}] {item['content']} [dim]({item['source']})[/dim]")
            continue
        if prompt.startswith("/forget "):
            identifier = prompt.removeprefix("/forget ").strip()
            if runtime.memory_store is not None:
                deleted = runtime.memory_store.forget(identifier)
            else:
                try:
                    deleted = runtime.store.delete_memory(int(identifier))
                except ValueError:
                    deleted = False
            console.print("已删除。" if deleted else "未找到该记忆。")
            continue
        if prompt == "/memory extract" or prompt.startswith("/memory extract "):
            if runtime.memory_extractor is None:
                console.print("[yellow]自动 Markdown 记忆未启用。[/yellow]")
                continue
            requested_run = prompt.removeprefix("/memory extract").strip() or None
            result = await runtime.memory_extractor.extract_pending(requested_run)
            console.print(
                "记忆提取完成："
                f"processed={result['processed']}，failed={result['failed']}，"
                f"added={result['added']}，merged={result['merged']}，"
                f"conflicts={result['conflicts']}，suppressed={result['suppressed']}。"
            )
            continue
        if prompt == "/new":
            session_id = runtime.store.create_session(runtime.workspace)
            console.print(f"已创建会话 {session_id[:8]}")
            continue
        clean_prompt, explicit_skills = _parse_prompt(prompt)
        if not clean_prompt:
            console.print("[yellow]请输入 Skill 后的任务内容。[/yellow]")
            continue
        result = await _run_with_steering(
            runtime,
            prompt_session,
            RunRequest(
                prompt=clean_prompt,
                session_id=session_id,
                explicit_skills=explicit_skills,
            ),
        )
        if result.status == "blocked":
            console.print(f"[yellow]{result.error or result.status}[/yellow]")
        elif result.status != "completed":
            console.print(f"[red]{result.error or result.status}[/red]")


async def _run_with_steering(runtime, prompt_session: PromptSession[str], request: RunRequest):
    run_task = asyncio.create_task(runtime.runner.run(request))
    approval_handler = runtime.approval_handler
    approval_task = (
        asyncio.create_task(approval_handler.next_request())
        if isinstance(approval_handler, InteractiveApprovalHandler)
        else None
    )
    input_task: asyncio.Task[str] | None = None
    try:
        while not run_task.done():
            if input_task is None:
                input_task = asyncio.create_task(prompt_session.prompt_async("[steer or /cancel] "))
            waiting: set[asyncio.Task] = {run_task, input_task}
            if approval_task:
                waiting.add(approval_task)
            with patch_stdout():
                done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
            if run_task in done:
                break
            if approval_task and approval_task in done:
                input_task.cancel()
                await asyncio.gather(input_task, return_exceptions=True)
                input_task = None
                pending = approval_task.result()
                with patch_stdout():
                    await approval_handler.resolve(pending, prompt_session)
                approval_task = asyncio.create_task(approval_handler.next_request())
                continue
            if input_task in done:
                try:
                    steering = input_task.result().strip()
                except EOFError:
                    input_task = None
                    return await run_task
                input_task = None
                if not steering:
                    continue
                if steering == "/cancel":
                    run_task.cancel()
                    break
                accepted = await runtime.runner.steer(request.session_id, steering)
                if accepted:
                    console.print("[dim]已加入当前运行，将在安全边界应用。[/dim]")
                else:
                    console.print("[yellow]当前运行已结束，输入未应用。[/yellow]")
        return await run_task
    finally:
        tasks = [task for task in (input_task, approval_task) if task]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@app.command("run")
def run_command(
    ctx: typer.Context,
    prompt: Annotated[str, typer.Argument(help="要执行的任务")],
    json_output: Annotated[bool, typer.Option("--json", help="输出 JSONL 事件")] = False,
) -> None:
    workspace = ctx.obj["workspace"]
    config_path = ctx.obj["config_path"]
    runtime = _runtime(
        workspace,
        config_path,
        json_output=json_output,
        interactive=sys.stdin.isatty() and not json_output,
    )
    try:
        clean_prompt, explicit_skills = _parse_prompt(prompt)
        request = RunRequest(
            prompt=clean_prompt,
            session_id=runtime.store.create_session(workspace),
            explicit_skills=explicit_skills,
            json_output=json_output,
        )
        if sys.stdin.isatty() and not json_output:
            result = _run(
                _with_runtime_shutdown(
                    runtime,
                    _run_with_steering(runtime, PromptSession(), request),
                )
            )
        else:
            result = _run(_with_runtime_shutdown(runtime, runtime.runner.run(request)))
        if result.status != "completed":
            if json_output:
                print(result.model_dump_json())
            elif result.status == "blocked":
                console.print(f"[yellow]{result.error or result.status}[/yellow]")
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
        _run(_with_runtime_shutdown(runtime, _interactive_loop(runtime, selected)))
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
        resolve_model_api_key(config.model, workspace=workspace)
        source = (
            "model.api_key（已脱敏）"
            if config.model.api_key
            else f"引用 {config.model.api_key_ref}"
        )
        console.print(f"[green]✓[/green] API Key 配置有效: {source}")
    except ConfigError as exc:
        errors += 1
        console.print(f"[red]✗[/red] {exc}")

    dotenv_path = workspace / ".env"
    if config.model.api_key:
        variable = None
    else:
        try:
            variable = api_key_reference_variable(config.model.api_key_ref)
        except ConfigError:
            variable = None
    if variable is not None and dotenv_path.is_file():
        try:
            dotenv_value = dotenv_values(dotenv_path).get(variable)
        except (OSError, UnicodeError):
            dotenv_value = None
        environment_value = os.environ.get(variable)
        if environment_value and dotenv_value and environment_value != dotenv_value:
            selected = ".env" if config.model.api_key_ref.startswith("dotenv:") else "环境变量"
            console.print(
                f"[yellow]![/yellow] 环境变量 {variable} 与 {dotenv_path} 中的值不同；"
                f"当前引用会使用{selected}，请确认这是预期行为"
            )
        if os.name == "posix":
            try:
                mode = stat.S_IMODE(dotenv_path.stat().st_mode)
            except OSError:
                mode = 0
            if mode & 0o077:
                console.print(
                    f"[yellow]![/yellow] {dotenv_path} 权限为 {mode:04o}，建议执行 chmod 600 .env"
                )

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
        store = SQLiteSessionStore(config.state_path(workspace))
        store.close()
        console.print(f"[green]✓[/green] 状态库可写: {config.state_path(workspace)}")
    except OSError as exc:
        errors += 1
        console.print(f"[red]✗[/red] 状态库不可写: {exc}")
    if config.memory.enabled:
        try:
            memory_path = config.memory_path(workspace)
            try:
                workspace.relative_to(memory_path)
            except ValueError:
                pass
            else:
                raise OSError("memory.path 不能等于工作区或位于工作区上层")
            memory_store = MarkdownMemoryStore(memory_path)
            console.print(f"[green]✓[/green] Markdown 记忆目录可写: {memory_store.root}")
        except OSError as exc:
            errors += 1
            console.print(f"[red]✗[/red] Markdown 记忆目录不可写: {exc}")
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
# 可选：直接保存模型凭据；工作区配置应保持 0600 且不得提交到 Git
# api_key = ""
api_key_ref = "auto:BOT_MODEL_API_KEY"
name = ""
context_window_tokens = 131072

[context]
compaction_strategy = "a_fallback"
max_input_tokens = 120000
auto_compact_threshold = 0.8
output_reserve_tokens = 4096
protocol_reserve_tokens = 2048
safety_margin_tokens = 2048
# 近期原文尾部按 token 有界；强制压缩收集到三条 user 即停，也不加入会使 tail 超预算的更旧组
recent_conversation_tokens = 20000
compaction_min_recent_user_turns = 3
# 仅旧 current/B 策略使用；双路径始终使用当前主模型
# compaction_model = ""
# 摘要长度软目标；发布时仍验证生成完整性与恢复后的总输入预算
compaction_summary_target_tokens = 3000
compaction_max_output_tokens = 8192
compaction_source_refs = "range"
# 双路径两次摘要的 thinking 都跟随 model.thinking；未设置时沿用供应商默认值
compaction_request_timeout_seconds = 90
compaction_repair_attempts = 1
compaction_transport_retries = 1
compaction_transport_retry_backoff_seconds = 1
compaction_condense_attempts = 1
compaction_empty_retries = 1
compaction_range_attempts = 2
compaction_failure_backoff_seconds = 300
compaction_command_max_requests = 8
compaction_command_max_seconds = 600
compaction_command_max_cost_usd = 0.25
compaction_rebuild_every = 5

[memory]
enabled = true
path = "./.bot/memory"
auto_extract = true
# 可选：单独指定记忆提取模型；留空则复用 model.name
# model = ""
max_runs_per_cycle = 3
max_candidates_per_run = 5
min_confidence = 0.75
index_tokens = 2000

[agent]
model_request_retries = 2
model_request_retry_backoff_seconds = 1

[subagents]
enabled = true
max_concurrent = 3
max_queued = 32
max_tasks_per_session = 16
allow_worktree_writes = true

[agents]
user_path = "~/.bot/agents"
project_path = ".bot/agents"
auto_resume_background = false
required_wait_timeout_seconds = 900

[permissions]
mode = "safe"
# 危险选项：自动批准所有 ASK；敏感路径、越界路径和非法策略绕过仍会拒绝
auto_approve = false
workspace_only = true
network = "ask"

[agent.progress]
enabled = true
warning_after_no_progress_steps = 4
recovery_after_no_progress_steps = 7
finalize_after_no_progress_steps = 11
max_recovery_attempts_per_epoch = 1
process_inactivity_warning_seconds = 300
process_inactivity_recovery_seconds = 900
# process_inactivity_finalize_seconds = 3600

[agent.finalization]
enabled = true
model_timeout_seconds = 120

[skills]
path = "./skills"
auto_activate = true
max_auto_activated = 3
"""
    target.write_text(template, encoding="utf-8")
    target.chmod(0o600)
    env_example = workspace / ".env.example"
    created_env_example = False
    if not env_example.exists():
        env_example.write_text(
            "# Copy this file to .env and replace the placeholder.\n"
            "BOT_MODEL_API_KEY=your-api-key\n",
            encoding="utf-8",
        )
        created_env_example = True
    installed_skills = _install_bundled_skills(workspace / "skills")
    console.print(f"[green]已创建[/green] {target}")
    if created_env_example:
        console.print(f"[green]已创建[/green] {env_example}")
    if installed_skills:
        console.print(f"[green]已安装内置 Skill[/green] {', '.join(installed_skills)}")


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


def _print_plan(plan: dict | None) -> None:
    if plan is None or not plan.get("items"):
        console.print("当前会话没有 TODO。")
        return
    table = Table("状态", "TODO")
    labels = {
        "pending": "○ pending",
        "in_progress": "● in progress",
        "completed": "✓ completed",
    }
    for item in plan["items"]:
        table.add_row(labels.get(str(item["status"]), str(item["status"])), str(item["content"]))
    if plan.get("explanation"):
        table.caption = str(plan["explanation"])
    console.print(table)


def _agent_catalog(workspace: Path, config, store: SQLiteSessionStore) -> AgentCatalog:
    tools = ToolRegistry()
    register_builtin_tools(tools)
    register_kunpeng_tools(tools)
    try:
        project_root = config.project_agent_path(workspace)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    digest = AgentCatalog.compute_project_digest(project_root)
    catalog = AgentCatalog(
        builtin_root=Path(__file__).resolve().parents[1] / "assets" / "agents",
        user_root=config.user_agent_path(),
        project_root=project_root,
        tools=tools,
        project_trusted=store.is_agent_workspace_trusted(workspace, digest),
        allow_worktree_writes=(
            config.subagents.allow_worktree_writes and config.permissions.mode != "read-only"
        ),
    )
    catalog.scan()
    return catalog


def _print_agent_diagnostics(catalog: AgentCatalog | None) -> None:
    if catalog is None:
        return
    for diagnostic in catalog.diagnostics:
        color = "red" if diagnostic.level == "error" else "yellow"
        console.print(
            f"[{color}]{diagnostic.level}[/{color}] {diagnostic.path}: {diagnostic.message}"
        )


def _print_agents(catalog: AgentCatalog | None) -> None:
    if catalog is None:
        console.print("Agent catalog 不可用。")
        return
    table = Table("名称", "来源", "模型", "隔离", "默认执行", "说明")
    for item in catalog.summary():
        table.add_row(
            item["name"],
            item["source"],
            item["model"],
            item["isolation"],
            item["default_execution"],
            item["description"],
        )
    console.print(table)
    _print_agent_diagnostics(catalog)


def _reload_runtime_agents(runtime) -> None:
    catalog = runtime.agent_catalog
    if catalog is None:
        raise RuntimeError("Agent catalog 不可用")
    digest = AgentCatalog.compute_project_digest(catalog.project_root)
    catalog.project_trusted = runtime.store.is_agent_workspace_trusted(runtime.workspace, digest)
    catalog.scan()
    specs = catalog.list()
    for spec in specs:
        step_limits = [
            value
            for value in (
                spec.max_steps,
                runtime.config.agent.max_steps,
                runtime.config.subagents.max_steps,
            )
            if value is not None
        ]
        spec.max_steps = min(step_limits) if step_limits else None
        wall_time_limits = [
            value
            for value in (
                spec.max_wall_time_seconds,
                runtime.config.agent.max_wall_time_seconds,
                runtime.config.subagents.max_wall_time_seconds,
            )
            if value is not None
        ]
        spec.max_wall_time_seconds = min(wall_time_limits) if wall_time_limits else None
        configured_costs = [
            value
            for value in (
                spec.max_cost_usd,
                runtime.config.agent.max_cost_usd,
                runtime.config.subagents.max_cost_usd_per_task,
                runtime.config.subagents.max_total_cost_usd_per_session,
            )
            if value is not None
        ]
        spec.max_cost_usd = min(configured_costs) if configured_costs else None
    runtime.subagents.replace_specs(specs)


@agent_app.command("list")
def agent_list(ctx: typer.Context) -> None:
    workspace = ctx.obj["workspace"].resolve()
    config = load_config(workspace, config_path=ctx.obj["config_path"])
    store = SQLiteSessionStore(config.state_path(workspace))
    try:
        _print_agents(_agent_catalog(workspace, config, store))
    finally:
        store.close()


@agent_app.command("show")
def agent_show(ctx: typer.Context, name: str) -> None:
    workspace = ctx.obj["workspace"].resolve()
    config = load_config(workspace, config_path=ctx.obj["config_path"])
    store = SQLiteSessionStore(config.state_path(workspace))
    try:
        catalog = _agent_catalog(workspace, config, store)
        spec = catalog.agents.get(name)
        if spec is None:
            console.print(f"[red]Agent 不存在、冲突或尚未信任：{name}[/red]")
            raise typer.Exit(1)
        console.print_json(spec.model_dump_json())
        console.print("\n" + spec.instructions, markup=False)
    finally:
        store.close()


@agent_app.command("validate")
def agent_validate(ctx: typer.Context) -> None:
    workspace = ctx.obj["workspace"].resolve()
    config = load_config(workspace, config_path=ctx.obj["config_path"])
    store = SQLiteSessionStore(config.state_path(workspace))
    try:
        catalog = _agent_catalog(workspace, config, store)
        _print_agents(catalog)
        if any(item.level == "error" for item in catalog.diagnostics):
            raise typer.Exit(1)
    finally:
        store.close()


@agent_app.command("trust")
def agent_trust(ctx: typer.Context) -> None:
    workspace = ctx.obj["workspace"].resolve()
    config = load_config(workspace, config_path=ctx.obj["config_path"])
    try:
        project_root = config.project_agent_path(workspace)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    digest = AgentCatalog.compute_project_digest(project_root)
    store = SQLiteSessionStore(config.state_path(workspace))
    try:
        store.trust_agent_workspace(workspace, digest)
    finally:
        store.close()
    console.print(f"[green]已信任[/green] {project_root} 当前内容摘要 {digest[:12]}")


@agent_app.command("untrust")
def agent_untrust(ctx: typer.Context) -> None:
    workspace = ctx.obj["workspace"].resolve()
    config = load_config(workspace, config_path=ctx.obj["config_path"])
    store = SQLiteSessionStore(config.state_path(workspace))
    try:
        store.untrust_agent_workspace(workspace)
    finally:
        store.close()
    console.print(f"[yellow]已取消信任[/yellow] {workspace}")


@session_app.command("list")
def session_list(ctx: typer.Context) -> None:
    workspace = ctx.obj["workspace"].resolve()
    config = load_config(workspace, config_path=ctx.obj["config_path"])
    store = SQLiteSessionStore(config.state_path(workspace))
    try:
        table = Table("Session", "Workspace", "Updated")
        for item in store.list_sessions():
            table.add_row(item["id"], item["workspace"], item["updated_at"])
        console.print(table)
    finally:
        store.close()


@session_app.command("show")
def session_show(ctx: typer.Context, session_id: str) -> None:
    workspace = ctx.obj["workspace"].resolve()
    config = load_config(workspace, config_path=ctx.obj["config_path"])
    store = SQLiteSessionStore(config.state_path(workspace))
    try:
        session = store.get_session(session_id)
        if not session:
            console.print(f"[red]会话不存在：{session_id}[/red]")
            raise typer.Exit(1)
        console.print(session)
        for position, message in enumerate(store.load_messages(session_id), 1):
            label = message.name or message.role.value
            console.print(f"[bold]{position}. {label}[/bold] {message.content or ''}")
    finally:
        store.close()


@trace_app.command("export")
def trace_export(
    events: Annotated[Path, typer.Argument(help="实时 JSONL 事件日志")],
    state: Annotated[Path, typer.Option("--state", help="导出的 SQLite 状态快照")],
    output: Annotated[Path, typer.Option("--output", "-o", help="Trace Bundle 输出目录")],
    prediction: Annotated[Path | None, typer.Option("--prediction")] = None,
    result: Annotated[Path | None, typer.Option("--result")] = None,
    setup_log: Annotated[Path | None, typer.Option("--setup-log")] = None,
    stderr_log: Annotated[Path | None, typer.Option("--stderr-log")] = None,
) -> None:
    try:
        manifest = export_trace_bundle(
            events_path=events,
            state_path=state,
            output_dir=output,
            prediction_path=prediction,
            result_path=result,
            setup_log_path=setup_log,
            stderr_log_path=stderr_log,
        )
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        console.print(f"[red]Trace 导出失败：{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print_json(json.dumps(manifest, ensure_ascii=False))


@trace_app.command("show")
def trace_show(bundle: Annotated[Path, typer.Argument(help="Trace Bundle 目录")]) -> None:
    transcript = bundle.resolve() / "transcript.md"
    if not transcript.is_file():
        console.print(f"[red]Trace transcript 不存在：{transcript}[/red]")
        raise typer.Exit(1)
    console.print(transcript.read_text(encoding="utf-8"), markup=False, highlight=False)


@session_app.command("fork")
def session_fork(
    ctx: typer.Context,
    session_id: str,
    up_to_position: Annotated[int | None, typer.Option("--at", help="只复制到指定消息位置")] = None,
) -> None:
    workspace = ctx.obj["workspace"].resolve()
    config = load_config(workspace, config_path=ctx.obj["config_path"])
    store = SQLiteSessionStore(config.state_path(workspace))
    try:
        try:
            new_session = store.fork_session(session_id, up_to_position=up_to_position)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        console.print(new_session)
    finally:
        store.close()


@config_app.command("get")
def config_get(
    ctx: typer.Context,
    key: Annotated[str | None, typer.Argument(help="可选的 section.key")] = None,
) -> None:
    workspace = ctx.obj["workspace"].resolve()
    config = load_config(workspace, config_path=ctx.obj["config_path"])
    data = config.model_dump(mode="json")
    data["model"]["api_key"] = "<redacted>" if config.model.api_key else None
    data["model"]["api_key_ref"] = config.model.api_key_ref
    try:
        value = get_config_value(data, key) if key else data
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print_json(json.dumps(value, ensure_ascii=False))


@config_app.command("set")
def config_set(ctx: typer.Context, key: str, value: str) -> None:
    workspace = ctx.obj["workspace"].resolve()
    target = config_target(workspace, ctx.obj["config_path"])
    try:
        set_config_value(target, key, parse_config_value(value))
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[green]已更新[/green] {target}: {key}")


@config_app.command("edit")
def config_edit(ctx: typer.Context) -> None:
    workspace = ctx.obj["workspace"].resolve()
    target = config_target(workspace, ctx.obj["config_path"])
    if not target.exists():
        console.print("[yellow]配置不存在，请先运行 bot init。[/yellow]")
        raise typer.Exit(1)
    editor = os.environ.get("EDITOR")
    if not editor:
        console.print("[red]环境变量 EDITOR 未设置。[/red]")
        raise typer.Exit(1)
    before = target.read_bytes()
    completed = subprocess.run([*shlex.split(editor), str(target)], check=False)
    if completed.returncode != 0:
        console.print(f"[red]编辑器退出码 {completed.returncode}[/red]")
        raise typer.Exit(completed.returncode)
    try:
        load_config(workspace, config_path=target)
    except ConfigError as exc:
        target.write_bytes(before)
        console.print(f"[red]配置无效，已恢复编辑前内容：{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[green]配置有效[/green] {target}")


@model_app.command("list")
def model_list(ctx: typer.Context) -> None:
    workspace = ctx.obj["workspace"].resolve()
    config = load_config(workspace, config_path=ctx.obj["config_path"])
    console.print(f"* {config.model.name or '<unset>'} @ {config.model.base_url or '<unset>'}")


@model_app.command("set")
def model_set(ctx: typer.Context, name: str) -> None:
    workspace = ctx.obj["workspace"].resolve()
    target = config_target(workspace, ctx.obj["config_path"])
    try:
        set_config_value(target, "model.name", name)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[green]模型已设置为[/green] {name}")


@eval_app.command("run")
def eval_run(
    ctx: typer.Context,
    cases_path: Annotated[Path, typer.Argument(help="JSONL Eval case 文件")],
    output: Annotated[Path | None, typer.Option("--output", "-o", help="结果 JSONL 文件")] = None,
    disable_skills: Annotated[
        bool, typer.Option("--disable-skills", help="关闭 Skill，作为对照组")
    ] = False,
    artifacts_dir: Annotated[
        Path | None, typer.Option("--artifacts-dir", help="逐次验收结果、清单和轨迹目录")
    ] = None,
) -> None:
    try:
        cases = load_eval_cases(cases_path.resolve())
    except (OSError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    async def execute_cases():
        results = []
        for case in cases:
            results.append(
                await run_eval_case(
                    case,
                    base=cases_path.resolve().parent,
                    config_path=ctx.obj["config_path"],
                    disable_skills=disable_skills,
                    artifacts_dir=artifacts_dir or ctx.obj["workspace"] / ".bot/evals",
                    config_workspace=ctx.obj["workspace"],
                )
            )
        return results

    results = _run(execute_cases())
    table = Table("Case", "Verdict", "Run", "Failure", "Steps", "Tools", "Tokens", "Cost", "Time")
    for result in results:
        table.add_row(
            result.id,
            result.verdict,
            result.run_status,
            result.failure_kind or "-",
            str(result.steps),
            ",".join(result.tool_names) or "-",
            f"{result.input_tokens}/{result.output_tokens}",
            f"${result.cost_usd:.6f}" if result.cost_usd is not None else "-",
            f"{result.duration_seconds:.2f}s",
        )
        for failure in result.failures:
            console.print(f"[red]{result.id}: {failure}[/red]")
        console.print(f"{result.id} 验收记录：{result.artifact_dir}")
    console.print(table)
    if output:
        write_eval_results(output.resolve(), results)
        console.print(f"结果已写入 {output.resolve()}")
    if any(not result.passed for result in results):
        raise typer.Exit(1)


# ── web command ───────────────────────────────────────────────────────────────


@web_app.command("start")
def web_start(
    ctx: typer.Context,
    host: Annotated[str, typer.Option("--host", "-h", help="监听地址")] = "0.0.0.0",
    port: Annotated[int, typer.Option("--port", "-p", help="监听端口")] = 8080,
) -> None:
    """启动 Web 界面服务器。"""
    try:
        import uvicorn  # noqa: F401
    except ImportError:
        console.print("[red]Web UI 需要额外依赖。请安装: pip install fastapi uvicorn[/red]")
        raise typer.Exit(1) from None

    workspace = ctx.obj["workspace"].resolve()
    config_path = ctx.obj.get("config_path")

    # Import here so the CLI help works even without web deps
    from bot.web.server import create_app

    web_app_instance = create_app(workspace=workspace, config_path=config_path)

    console.print(
        f"[green]Bot Web UI 启动中...[/green]\n  http://{host}:{port}\n  工作区: {workspace}"
    )

    uvicorn.run(
        web_app_instance,
        host=host,
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    app()

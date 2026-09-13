import asyncio
import json

import pytest

from bot.compaction import ContextCompactor
from bot.config.models import AppConfig
from bot.core.agent import AgentRunner
from bot.core.context import ContextAssembler
from bot.core.events import EventBus
from bot.core.models import (
    ChatMessage,
    ModelCapabilities,
    ModelEvent,
    ModelEventKind,
    Role,
    RunRequest,
)
from bot.execution.local import LocalExecutionTarget
from bot.policy import DefaultPolicyEngine
from bot.providers import ModelProvider, ProviderError, ProviderErrorKind
from bot.sessions import SQLiteSessionStore
from bot.skills import SkillCatalog, SkillManager
from bot.tools import ToolRegistry
from bot.tools.builtins import ReadFileTool


def finish(text="done"):
    return [
        ModelEvent(kind=ModelEventKind.TEXT_DELTA, text=text),
        ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
    ]


def calls(*specs):
    events = [
        ModelEvent(
            kind=ModelEventKind.TOOL_CALL_DELTA,
            tool_index=index,
            tool_call_id=call_id,
            tool_name=name,
            arguments_delta=json.dumps(arguments),
        )
        for index, (call_id, name, arguments) in enumerate(specs)
    ]
    return events + [ModelEvent(kind=ModelEventKind.FINISH, finish_reason="tool_calls")]


class SkillProvider(ModelProvider):
    def __init__(self, turns):
        self.turns = turns
        self.requests = []
        self.on_request = None

    def capabilities(self, model):
        return ModelCapabilities()

    async def stream(self, request):
        self.requests.append(request.model_copy(deep=True))
        if self.on_request is not None:
            await self.on_request(request)
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        for event in turn:
            yield event


class SummaryProvider(ModelProvider):
    def __init__(self):
        self.requests = []

    def capabilities(self, model):
        return ModelCapabilities()

    async def stream(self, request):
        self.requests.append(request)
        payload = json.loads(request.messages[-1].content)
        start, end = payload["covered_range"]
        text = "\n\n".join(
            f"# {section}\n- 已记录任务进度，继续验证已有证据。 [m:{start}-{end}]"
            for section in [
                "Goal",
                "Constraints",
                "Progress",
                "Key Decisions",
                "Relevant Files",
                "Failures",
                "Next Steps",
                "Critical Context",
            ]
        )
        for event in finish(text):
            yield event


def write_skill(root, name="analysis", body="Follow UNIQUE_SKILL_RULE."):
    directory = root / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(f"---\nname: {name}\ndescription: Evidence analysis.\n---\n{body}")
    return path


def make_runner(root, provider, *, context=None, agent=None, skills=None, compaction=False):
    config = AppConfig.model_validate(
        {
            "model": {"name": "mock", "base_url": "https://unused"},
            "skills": {"path": str(root / "skills"), **(skills or {})},
            # The separate summary provider implements the legacy protocol.
            "context": {"compaction_strategy": "current", **(context or {})},
            "agent": agent or {},
        }
    )
    catalog = SkillCatalog(root / "skills")
    catalog.scan()
    store = SQLiteSessionStore(root / "state.db")
    bus = EventBus([store])
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    runner = AgentRunner(
        config=config,
        workspace=root,
        provider=provider,
        tool_registry=registry,
        policy=DefaultPolicyEngine(config.permissions, root),
        execution_target=LocalExecutionTarget(),
        skills=SkillManager(catalog, config.skills.max_auto_activated),
        context=ContextAssembler(workspace=root, skill_catalog=catalog),
        store=store,
        event_bus=bus,
        context_compactor=(
            ContextCompactor(config=config, provider=SummaryProvider(), store=store, event_bus=bus)
            if compaction
            else None
        ),
    )
    return runner, store


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [True, False])
async def test_long_skill_is_delivered_once_without_preview_truncation(tmp_path, explicit):
    body = "BEGIN_RULE\n" + "规则内容甲乙丙丁。\n" * 1400 + "\nTAIL_RULE_947"
    assert len(body) > 12_000
    write_skill(tmp_path, body=body)
    (tmp_path / "evidence.txt").write_text("verified")
    turns = (
        []
        if explicit
        else [calls(("activate", "activate_skill", {"name": "analysis", "reason": "analyze"}))]
    )
    turns += [calls(("read", "read_file", {"path": "evidence.txt"})), finish()]
    provider = SkillProvider(turns)
    runner, store = make_runner(tmp_path, provider, context={"tool_result_inline_tokens": 100})
    try:
        result = await runner.run(
            RunRequest(prompt="analyze", explicit_skills=["analysis"] if explicit else [])
        )
        assert result.status == "completed", result.error
        for request in provider.requests[0 if explicit else 1 :]:
            matches = [m for m in request.messages if "TAIL_RULE_947" in (m.content or "")]
            assert len(matches) == 1
            text = json.loads(matches[0].content)["content"] if explicit else matches[0].content
            assert body in text
            assert not any(m.name == "active_skill" for m in request.messages)
        deliveries = [
            e for e in store.load_positioned_messages(result.session_id) if e.skill_delivery
        ]
        assert len(deliveries) == 1
        assert not deliveries[0].is_real_user
        assert deliveries[0].skill_delivery.matches(deliveries[0].message)
        assert not runner.active_skill_names(result.session_id)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_repeated_activation_reuses_history_across_run_and_fork(tmp_path):
    write_skill(tmp_path)

    def activation(call_id):
        return calls((call_id, "activate_skill", {"name": "analysis", "reason": "needed"}))

    provider = SkillProvider(
        [activation("first"), activation("again"), finish(), finish(), finish()]
    )
    runner, store = make_runner(tmp_path, provider)
    try:
        first = await runner.run(RunRequest(prompt="analyze"))
        assert first.status == "completed"
        second = await runner.run(
            RunRequest(prompt="continue", session_id=first.session_id, explicit_skills=["analysis"])
        )
        fork = store.fork_session(first.session_id)
        third = await runner.run(
            RunRequest(prompt="continue", session_id=fork, explicit_skills=["analysis"])
        )
        assert second.status == third.status == "completed"
        for request in provider.requests[1:]:
            assert sum("UNIQUE_SKILL_RULE" in (m.content or "") for m in request.messages) == 1
        for session in [first.session_id, fork]:
            assert (
                len([e for e in store.load_positioned_messages(session) if e.skill_delivery]) == 1
            )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_unrelated_run_cannot_read_resources_without_new_binding(tmp_path):
    path = write_skill(tmp_path)
    (path.parent / "references").mkdir()
    (path.parent / "references" / "data.txt").write_text("RESOURCE_SECRET_RULE")
    provider = SkillProvider(
        [
            finish(),
            calls(
                (
                    "resource",
                    "load_skill_resource",
                    {"skill": "analysis", "path": "references/data.txt"},
                )
            ),
            finish(),
        ]
    )
    runner, store = make_runner(tmp_path, provider)
    try:
        first = await runner.run(RunRequest(prompt="analyze", explicit_skills=["analysis"]))
        second = await runner.run(RunRequest(prompt="unrelated", session_id=first.session_id))
        assert second.status == "completed"
        resource = next(m for m in provider.requests[-1].messages if m.tool_call_id == "resource")
        assert "尚未激活" in resource.content
        assert "RESOURCE_SECRET_RULE" not in resource.content
        assert [t.to_openai() for t in provider.requests[0].tools] == [
            t.to_openai() for t in provider.requests[1].tools
        ]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_compaction_restores_exact_version_and_excludes_synthetic_user_anchors(tmp_path):
    path = write_skill(tmp_path, body="OLD_VERSION_947\n" + "流程规则。" * 600 + "\nOLD_TAIL_947")
    (tmp_path / "evidence.txt").write_text("read evidence " * 200)
    provider = SkillProvider(
        [
            calls(("read1", "read_file", {"path": "evidence.txt"})),
            calls(("read2", "read_file", {"path": "evidence.txt"})),
            finish(),
        ]
    )
    runner, store = make_runner(
        tmp_path, provider, compaction=True, context={"recent_conversation_tokens": 300}
    )

    async def force_compaction(request):
        if len(provider.requests) <= 2:
            session_id = next(iter(runner._run_skill_states))
            runner.request_compaction(session_id)
            if len(provider.requests) == 1:
                path.write_text("---\nname: analysis\ndescription: New version.\n---\nNEW_RULE")
                runner.skills.catalog.scan()

    provider.on_request = force_compaction
    try:
        result = await runner.run(
            RunRequest(prompt="original user task", explicit_skills=["analysis"])
        )
        assert result.status == "completed", result.error
        for request in provider.requests:
            matches = [m for m in request.messages if "OLD_TAIL_947" in (m.content or "")]
            assert len(matches) == 1
            assert "NEW_RULE" not in "\n".join(m.content or "" for m in request.messages)
        entries = store.load_positioned_messages(result.session_id)
        body_entries = [e for e in entries if e.skill_delivery and e.skill_delivery.is_body]
        assert [e.skill_delivery.kind for e in body_entries] == [
            "explicit_body",
            "restored_body",
            "restored_body",
        ]
        assert len({e.skill_delivery.version_hash for e in body_entries}) == 1
        for compaction in store.list_context_compactions(result.session_id):
            assert all(entries[p - 1].is_real_user for p in compaction["anchor_positions"])
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["a", "b"])
@pytest.mark.parametrize("delivery", ["file", "reference"])
async def test_real_runner_strategy_restores_skill_and_continues_tools(
    tmp_path, strategy, delivery
):
    write_skill(tmp_path, body="KEEP_EXACT_SKILL_947\n" + "rules for evidence.\n" * 100)
    (tmp_path / "evidence.txt").write_text("verified current workspace")

    class CombinedProvider(SkillProvider):
        async def stream(self, request):
            last = request.messages[-1].content or ""
            is_summary = (
                "在当前安全断点" in last or '"kind": "leaf"' in last or '"kind": "merge"' in last
            )
            if is_summary:
                summary = "\n\n".join(
                    f"# {section}\n- Continue the evidence task."
                    for section in [
                        "Goal",
                        "Constraints",
                        "Progress",
                        "Key Decisions",
                        "Relevant Files",
                        "Failures",
                        "Next Steps",
                        "Critical Context",
                    ]
                )
                yield ModelEvent(kind=ModelEventKind.USAGE, input_tokens=100, output_tokens=20)
                for event in finish(summary):
                    yield event
            else:
                async for event in super().stream(request):
                    yield event

    provider = CombinedProvider([calls(("read1", "read_file", {"path": "evidence.txt"})), finish()])
    runner, store = make_runner(
        tmp_path,
        provider,
        compaction=True,
        context={
            "compaction_strategy": strategy,
            "recent_conversation_tokens": 300,
            "compaction_leaf_input_tokens": 4000,
        },
    )
    session = store.create_session(tmp_path)
    store.start_run(session, "history")
    for index in range(6):
        store.append_message(
            session,
            "history",
            ChatMessage(
                role=Role.ASSISTANT,
                content=f"inspection {index}: " + "old observations " * 700,
            ),
        )
    store.finish_run("history", "completed")
    if delivery == "reference":
        reference = store.put_context_blob(
            session_id=session, run_id="history", content="LIVE_PAYLOAD_852\n" * 400
        )
        provider.turns[0] = calls(
            ("read1", "load_context_reference", {"reference": reference, "limit": 16000})
        )

    async def request_cut(request):
        if len(provider.requests) == 1:
            runner.request_compaction(session)
        elif delivery == "reference":
            strategy_state = runner._run_compaction_strategies[session]
            active = runner.context_compactor.projection(session)["compaction"]
            projected = strategy_state.frame.project(active)
            assert "LIVE_PAYLOAD_852" in "\n".join(m.content or "" for m in projected.messages)
            assert "LIVE_PAYLOAD_852" in "\n".join(m.content or "" for m in request.messages)

    provider.on_request = request_cut
    try:
        result = await runner.run(
            RunRequest(
                session_id=session,
                prompt="Read evidence and conclude.",
                explicit_skills=["analysis"],
            )
        )
        assert result.status == "completed", result.error
        records = store.list_context_compactions(session)
        assert len(records) == 1 and records[0]["trigger"] == f"strategy:{strategy}"
        assert result.input_tokens >= 100 and result.output_tokens >= 20
        assert len(provider.requests) == 2
        for request in provider.requests:
            assert sum("KEEP_EXACT_SKILL_947" in (m.content or "") for m in request.messages) == 1
        entries = store.load_positioned_messages(session)
        assert any(e.skill_delivery and e.skill_delivery.kind == "restored_body" for e in entries)
        assert all(entries[p - 1].is_real_user for p in records[0]["anchor_positions"])
        assert not runner._run_compaction_strategies
    finally:
        store.close()


@pytest.mark.asyncio
async def test_resource_and_body_survive_provider_overflow_retry(tmp_path):
    path = write_skill(tmp_path)
    (path.parent / "references").mkdir()
    (path.parent / "references" / "data.txt").write_text("a" * 3000 + "RESOURCE_TAIL_947")
    provider = SkillProvider(
        [
            calls(
                (
                    "resource",
                    "load_skill_resource",
                    {"skill": "analysis", "path": "references/data.txt"},
                )
            ),
            ProviderError("too many tokens", kind=ProviderErrorKind.CONTEXT_LENGTH),
            finish(),
        ]
    )
    runner, store = make_runner(tmp_path, provider)
    try:
        result = await runner.run(RunRequest(prompt="analyze", explicit_skills=["analysis"]))
        assert result.status == "completed", result.error
        for request in provider.requests[1:]:
            text = "\n".join(m.content or "" for m in request.messages)
            assert "RESOURCE_TAIL_947" in text and "UNIQUE_SKILL_RULE" in text
        assert (
            len(
                [
                    e
                    for e in store.load_positioned_messages(result.session_id)
                    if e.skill_delivery and e.skill_delivery.kind == "resource"
                ]
            )
            == 1
        )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_finalizer_reuses_visible_body_after_compaction_without_restoring_it(tmp_path):
    write_skill(tmp_path)
    (tmp_path / "evidence.txt").write_text("done")
    provider = SkillProvider(
        [calls(("read", "read_file", {"path": "evidence.txt"})), finish("partial")]
    )
    runner, store = make_runner(tmp_path, provider, agent={"max_steps": 1}, compaction=True)

    async def compact_during_first_request(request):
        if len(provider.requests) == 1:
            session_id = next(iter(runner._run_skill_states))
            result = await runner.context_compactor.compact(
                session_id,
                through_position=2,
                active_run_ids={runner._run_skill_states[session_id].run_id},
                trigger="test",
            )
            assert result.compacted

    provider.on_request = compact_during_first_request
    try:
        result = await runner.run(RunRequest(prompt="analyze", explicit_skills=["analysis"]))
        assert result.status == "limit_reached"
        assert len(provider.requests) == 2
        assert provider.requests[-1].tools == provider.requests[0].tools
        assert provider.requests[-1].tool_choice == "none"
        assert provider.requests[-1].messages[: len(provider.requests[0].messages)] == (
            provider.requests[0].messages
        )
        assert any("UNIQUE_SKILL_RULE" in (m.content or "") for m in provider.requests[-1].messages)
        assert not any(
            e.skill_delivery and e.skill_delivery.kind == "restored_body"
            for e in store.load_positioned_messages(result.session_id)
        )
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["history", "legacy"])
async def test_finalizer_includes_skill_activated_after_last_main_request(tmp_path, mode):
    write_skill(tmp_path)
    provider = SkillProvider(
        [
            calls(("activate", "activate_skill", {"name": "analysis", "reason": "analyze"})),
            finish("activation summary"),
        ]
    )
    runner, store = make_runner(
        tmp_path, provider, agent={"max_steps": 1}, skills={"context_mode": mode}
    )
    try:
        result = await runner.run(RunRequest(prompt="analyze"))
        assert result.final_text == "activation summary"
        main, final = provider.requests
        assert "UNIQUE_SKILL_RULE" not in str(main.messages)
        assert "UNIQUE_SKILL_RULE" in str(final.messages)
        assert final.tools == main.tools and final.tool_choice == "none"
        if mode == "history":
            assert final.messages[: len(main.messages)] == main.messages
    finally:
        store.close()


@pytest.mark.asyncio
async def test_finalizer_delivers_pending_skill_resource_after_last_main_request(tmp_path):
    write_skill(tmp_path)
    resource = tmp_path / "skills" / "analysis" / "references" / "evidence.txt"
    resource.parent.mkdir()
    resource.write_text("PENDING_SKILL_RESOURCE")
    provider = SkillProvider(
        [
            calls(
                (
                    "resource",
                    "load_skill_resource",
                    {"skill": "analysis", "path": "references/evidence.txt"},
                )
            ),
            finish("resource summary"),
        ]
    )
    runner, store = make_runner(tmp_path, provider, agent={"max_steps": 1})
    try:
        result = await runner.run(RunRequest(prompt="analyze", explicit_skills=["analysis"]))
        assert result.final_text == "resource summary"
        main, final = provider.requests
        assert final.messages[: len(main.messages)] == main.messages
        assert "PENDING_SKILL_RESOURCE" in str(final.messages)
        assert "UNIQUE_SKILL_RULE" in str(final.messages)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_adapter_truncation_is_rejected_before_sampling(tmp_path):
    write_skill(tmp_path)

    class TruncatingProvider(SkillProvider):
        def serialized_messages(self, request):
            messages = super().serialized_messages(request)
            for message in messages:
                if "UNIQUE_SKILL_RULE" in message.get("content", ""):
                    message["content"] = "truncated"
            return messages

    provider = TruncatingProvider([])
    runner, store = make_runner(tmp_path, provider)
    try:
        result = await runner.run(RunRequest(prompt="analyze", explicit_skills=["analysis"]))
        assert result.termination_reason == "skill_body_not_visible"
        assert not provider.requests
        assert not runner.is_session_running(result.session_id)
        assert not runner.active_skill_names()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_concurrent_run_cancellation_preserves_other_binding(tmp_path):
    write_skill(tmp_path, "alpha", "ALPHA_RULE")
    write_skill(tmp_path, "beta", "BETA_RULE")
    both_started = asyncio.Event()
    release_beta = asyncio.Event()
    provider = SkillProvider([finish()])
    runner, store = make_runner(tmp_path, provider)
    alpha = store.create_session(tmp_path)
    beta = store.create_session(tmp_path)

    async def hold(request):
        if len(provider.requests) == 2:
            both_started.set()
        if any("ALPHA_RULE" in (m.content or "") for m in request.messages):
            await asyncio.Event().wait()
        else:
            await release_beta.wait()

    provider.on_request = hold
    a = asyncio.create_task(
        runner.run(RunRequest(prompt="alpha", session_id=alpha, explicit_skills=["alpha"]))
    )
    b = asyncio.create_task(
        runner.run(RunRequest(prompt="beta", session_id=beta, explicit_skills=["beta"]))
    )
    try:
        await asyncio.wait_for(both_started.wait(), 2)
        assert runner.active_skill_names(alpha) == ["alpha"]
        assert runner.active_skill_names(beta) == ["beta"]
        a.cancel()
        assert (await a).status == "cancelled"
        assert runner.active_skill_names(alpha) == []
        assert runner.active_skill_names(beta) == ["beta"]
        release_beta.set()
        assert (await b).status == "completed"
        assert not runner.active_skill_names()
        for request in provider.requests:
            text = "\n".join(m.content or "" for m in request.messages)
            assert ("ALPHA_RULE" in text) != ("BETA_RULE" in text)
    finally:
        for task in [a, b]:
            if not task.done():
                task.cancel()
        await asyncio.gather(a, b, return_exceptions=True)
        store.close()


@pytest.mark.asyncio
async def test_persistence_failure_releases_run_admission(tmp_path, monkeypatch):
    write_skill(tmp_path)
    provider = SkillProvider([finish()])
    runner, store = make_runner(tmp_path, provider)
    session = store.create_session(tmp_path)

    def fail(*args, **kwargs):
        raise RuntimeError("finish failed")

    monkeypatch.setattr(store, "finish_run", fail)
    try:
        with pytest.raises(RuntimeError, match="finish failed"):
            await runner.run(
                RunRequest(prompt="analyze", session_id=session, explicit_skills=["analysis"])
            )
        assert not runner.active_skill_names()
        assert not runner.is_session_running(session)
        await asyncio.wait_for(runner.wait_until_idle(session), 1)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_skill_delivery_keeps_sibling_tool_results_in_the_same_batch(tmp_path):
    write_skill(tmp_path)
    (tmp_path / "evidence.txt").write_text("sibling evidence " * 1000)
    provider = SkillProvider(
        [
            calls(
                ("activate", "activate_skill", {"name": "analysis", "reason": "needed"}),
                ("read", "read_file", {"path": "evidence.txt"}),
            ),
            finish(),
        ]
    )
    runner, store = make_runner(tmp_path, provider, context={"tool_result_inline_tokens": 100})
    try:
        result = await runner.run(RunRequest(prompt="analyze"))
        assert result.status == "completed", result.error
        messages = provider.serialized_messages(provider.requests[-1])
        start = next(i for i, m in enumerate(messages) if m.get("tool_calls"))
        assert {c["id"] for c in messages[start]["tool_calls"]} == {"activate", "read"}
        assert {m["tool_call_id"] for m in messages[start + 1 : start + 3]} == {
            "activate",
            "read",
        }
        assert "UNIQUE_SKILL_RULE" in messages[start + 1]["content"]
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", ["body", "request"])
async def test_insufficient_skill_budget_stops_before_sampling(tmp_path, budget):
    write_skill(tmp_path)
    provider = SkillProvider([])
    runner, store = make_runner(
        tmp_path,
        provider,
        context={"active_skill_tokens": 1} if budget == "body" else {"max_input_tokens": 100},
    )
    try:
        result = await runner.run(RunRequest(prompt="analyze", explicit_skills=["analysis"]))
        assert result.termination_reason == "skill_context_budget_exceeded"
        assert result.status == "limit_reached"
        assert not provider.requests
        assert not runner.active_skill_names()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_missing_body_after_compaction_stops_execution(tmp_path):
    write_skill(tmp_path)
    (tmp_path / "evidence.txt").write_text("evidence")
    provider = SkillProvider([calls(("read", "read_file", {"path": "evidence.txt"}))])
    runner, store = make_runner(
        tmp_path, provider, compaction=True, context={"recent_conversation_tokens": 1}
    )

    async def revoke_and_compact(request):
        state = next(iter(runner._run_skill_states.values()))
        reference = state.bindings["analysis"].body_ref
        store._connection.execute("DELETE FROM context_blob_access WHERE blob_id = ?", (reference,))
        store._connection.commit()
        runner.request_compaction(state.session_id)

    provider.on_request = revoke_and_compact
    try:
        result = await runner.run(RunRequest(prompt="analyze", explicit_skills=["analysis"]))
        assert result.termination_reason == "skill_body_unavailable", result.error
        assert result.status == "failed"
        assert len(provider.requests) == 1
        assert not runner.active_skill_names()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_provider_overflow_has_one_repair_and_no_finalizer_sampling(tmp_path):
    write_skill(tmp_path)
    provider = SkillProvider(
        [ProviderError("too many tokens", kind=ProviderErrorKind.CONTEXT_LENGTH) for _ in range(2)]
    )
    runner, store = make_runner(tmp_path, provider)
    try:
        result = await runner.run(RunRequest(prompt="analyze", explicit_skills=["analysis"]))
        assert result.status == "failed"
        assert result.termination_reason == "provider_error"
        assert len(provider.requests) == 2
        assert not runner.active_skill_names()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_adapter_cannot_orphan_an_intact_skill_result(tmp_path):
    write_skill(tmp_path)

    class OrphaningProvider(SkillProvider):
        def serialized_messages(self, request):
            return [m for m in super().serialized_messages(request) if not m.get("tool_calls")]

    provider = OrphaningProvider(
        [calls(("activate", "activate_skill", {"name": "analysis", "reason": "needed"}))]
    )
    runner, store = make_runner(tmp_path, provider)
    try:
        result = await runner.run(RunRequest(prompt="analyze"))
        assert result.termination_reason == "skill_body_not_visible"
        assert len(provider.requests) == 1
        assert not runner.active_skill_names()
    finally:
        store.close()

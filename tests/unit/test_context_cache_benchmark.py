from __future__ import annotations

from bot.core.context import TokenEstimator
from bot.core.models import ChatMessage, ModelRequest, Role, ToolDefinition
from bot.evals.context_cache import PrefixCacheSimulator


def test_prefix_cache_simulator_distinguishes_append_from_early_rewrite() -> None:
    simulator = PrefixCacheSimulator(minimum_cacheable_tokens=0)
    estimator = TokenEstimator()
    tool = ToolDefinition(
        name="stable_tool",
        description="stable",
        input_schema={"type": "object", "properties": {}},
    )
    system = ChatMessage(role=Role.SYSTEM, content="stable system")
    first_user = ChatMessage(role=Role.USER, content="turn one")
    first = ModelRequest(model="benchmark", messages=[system, first_user], tools=[tool])

    cold = simulator.observe(first)
    assert cold.cache_read_tokens == 0
    assert cold.first_changed_segment == "cold_start"

    response = ChatMessage(role=Role.ASSISTANT, content="turn one complete")
    simulator.commit(first, response)
    appended = ModelRequest(
        model="benchmark",
        messages=[
            system,
            first_user,
            response,
            ChatMessage(role=Role.USER, content="turn two"),
        ],
        tools=[tool],
    )
    append_hit = simulator.observe(appended)
    expected_append_prefix = (
        estimator.tool(tool)
        + estimator.message(system)
        + estimator.message(first_user)
        + estimator.message(response)
    )
    assert append_hit.cache_read_tokens == expected_append_prefix
    assert append_hit.cache_miss_tokens == estimator.message(appended.messages[-1])
    assert append_hit.first_changed_segment.startswith("user_message")

    rewritten = ModelRequest(
        model="benchmark",
        messages=[system, ChatMessage(role=Role.USER, content="rewritten turn one")],
        tools=[tool],
    )
    rewrite_hit = simulator.observe(rewritten)
    assert rewrite_hit.cache_read_tokens == estimator.tool(tool) + estimator.message(system)
    assert rewrite_hit.cache_read_tokens < append_hit.cache_read_tokens
    assert rewrite_hit.first_changed_segment.startswith("user_message")


def test_prefix_cache_simulator_reports_minimum_cache_unit_as_avoidable_miss() -> None:
    simulator = PrefixCacheSimulator(minimum_cacheable_tokens=10_000)
    request = ModelRequest(
        model="benchmark",
        messages=[ChatMessage(role=Role.SYSTEM, content="stable")],
    )
    simulator.commit(request)

    observation = simulator.observe(
        ModelRequest(
            model="benchmark",
            messages=[
                ChatMessage(role=Role.SYSTEM, content="stable"),
                ChatMessage(role=Role.USER, content="next"),
            ],
        )
    )

    assert observation.ideal_reusable_tokens > 0
    assert observation.cache_read_tokens == 0
    assert observation.avoidable_miss_tokens == observation.ideal_reusable_tokens

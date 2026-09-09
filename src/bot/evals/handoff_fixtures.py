"""Frozen, independently scored checkpoints for the handoff mechanism experiment."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from bot.core.models import ChatMessage, Role, ToolCall


@dataclass
class Checkpoint:
    name: str
    kind: str
    messages: list[ChatMessage]
    probes: list[dict]
    provenance: dict

    def serializable(self):
        return {
            "name": self.name,
            "kind": self.kind,
            "messages": [m.model_dump(mode="json") for m in self.messages],
            "probes": self.probes,
            "provenance": self.provenance,
        }

    @property
    def sha256(self):
        return hashlib.sha256(
            json.dumps(self.serializable(), sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()


def probe(question, expected, *, read_position=None):
    prompt = question + "\n只输出一个 JSON 对象，不要代码围栏，字段名称按问题要求。"
    if read_position is not None:
        prompt = (
            f"先调用 checkpoint_evidence，position={read_position}，确认原始记录后回答。\n" + prompt
        )
    return {"prompt": prompt, "expected": expected, "required_read": read_position}


def synthetic_checkpoint(name: str, blocks: int, rows: int, *, correction=False) -> Checkpoint:
    messages = [
        ChatMessage(
            role=Role.USER,
            content=(
                "修复 ledger_import 增量数据导入，禁止访问网络，禁止修改来源数据。"
                "原始要求 retry_limit=7；保持输出行顺序。提交前需验证当前实现版本。"
            ),
        )
    ]
    facts = [
        "首次 full_scan 尝试失败：内存超限。接下来不要重复 full_scan，使用 incremental_scan。",
        "当前代码文件 importer.py，版本 revision=2。"
        "旧 revision=1 的测试通过，不能证明 revision=2 通过。",
        "数据源 dataset_id=ledger-west-042，输出必须保持来源行顺序，不可按金额排序。",
        "排查假设：cache_staleness 可能导致重复行，目前没有足够证据，不能写成已证实原因。",
        "增量扫描已实现，当前版本尚未测试；下一步 run_incremental_checks。不要声称任务已完成。",
    ]
    for block in range(blocks):
        call = ToolCall(id=f"inspect-{block}", name="inspect_batch", arguments={"batch": block})
        messages.append(
            ChatMessage(role=Role.ASSISTANT, content="核对分区和执行记录。", tool_calls=[call])
        )
        lines = [f"Batch {block} observed records, source order retained:"]
        for row in range(rows):
            identity = block * rows + row
            # Distinct structured records, not a repeated padding paragraph.
            lines.append(
                f"row={identity:07d} account=AC{identity % 997:04d} "
                f"cents={(identity * 137) % 99991:05d} "
                f"partition={block:03d} source_seq={identity:07d}"
            )
        lines.insert(len(lines) // 2, facts[min(4, block * 5 // max(1, blocks))])
        messages.append(
            ChatMessage(
                role=Role.TOOL, name="inspect_batch", tool_call_id=call.id, content="\n".join(lines)
            )
        )
        if correction and block == blocks // 2:
            messages.append(
                ChatMessage(
                    role=Role.USER,
                    content="更正：retry_limit 改为 11，替代原来的 7。其他约束保持。",
                )
            )
    messages.append(
        ChatMessage(role=Role.ASSISTANT, content="扫描材料已经收集，当前实现尚待验证。")
    )
    limit = 11 if correction else 7
    probes = [
        probe(
            "来源任务是否允许网络和修改源数据？"
            "返回 network_allowed、source_mutation_allowed 布尔值。",
            {"network_allowed": False, "source_mutation_allowed": False},
            read_position=1,
        ),
        probe("当前有效 retry_limit 是多少？返回 retry_limit 整数。", {"retry_limit": limit}),
        probe(
            "当前代码文件和版本是什么？返回 path 字符串、revision 整数。",
            {"path": "importer.py", "revision": 2},
        ),
        probe(
            "此前哪一种扫描失败，失败原因是什么？"
            "返回 failed_method（工具名）、failure（英文蛇形命名）。",
            {"failed_method": "full_scan", "failure": "memory_limit"},
        ),
        probe(
            "当前版本测试已通过吗？返回 current_revision_verified 布尔值。",
            {"current_revision_verified": False},
        ),
        probe("cache_staleness 是否已被证实为根因？返回 proven 布尔值。", {"proven": False}),
        probe(
            "数据源 ID、是否保留来源行顺序及下一步是什么？"
            "返回 dataset_id、preserve_source_order 布尔值、next_action（原动作名）。",
            {
                "dataset_id": "ledger-west-042",
                "preserve_source_order": True,
                "next_action": "run_incremental_checks",
            },
        ),
        probe(
            "读取首条原始要求，返回 original_retry_limit 整数；注意这是历史原值。",
            {"original_retry_limit": 7},
            read_position=1,
        ),
    ]
    return Checkpoint(
        name,
        "synthetic-structured-log",
        messages,
        probes,
        {
            "generated": True,
            "blocks": blocks,
            "rows_per_block": rows,
            "purpose": "capacity and evidence retention; not a public task score",
            "workspace": "immutable message evidence fixture; no original application runtime",
        },
    )


def historical_checkpoints(root: Path) -> list[Checkpoint]:
    metrics = json.loads((root / "docs/data/public-benchmark-context-metrics.json").read_text())
    result = []
    for run in metrics["runs"]:
        for cp in run["compactions"]:
            path = root / run["state_path"]
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
                rows = db.execute(
                    "SELECT position, message_json FROM messages "
                    "WHERE position<=? ORDER BY position",
                    (cp["covered_end_position"],),
                ).fetchall()
            assert [row[0] for row in rows] == list(range(1, cp["covered_end_position"] + 1))
            messages = [ChatMessage.model_validate_json(row[1]) for row in rows]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == before
            if "chess" in run["task"]:
                name = "chess-large-tail"
                probes = [
                    probe(
                        "原用户指定的结果文件是什么？返回 output_path。",
                        {"output_path": "/app/move.txt"},
                        read_position=1,
                    ),
                    probe("当前哪方走棋？返回 side，使用 white 或 black。", {"side": "white"}),
                    probe("棋盘输入图像文件名是什么？返回 image。", {"image": "chess_board.png"}),
                    probe(
                        "如果存在多个获胜着法，应每行一个还是只保留一个？"
                        "返回 one_per_line 布尔值。",
                        {"one_per_line": True},
                    ),
                    probe(
                        "在交接前最后的分析中，尚需可靠区分哪两种王室棋子？"
                        "返回 pieces 数组，使用英文小写并按字母排序。",
                        {"pieces": ["king", "queen"]},
                    ),
                    probe(
                        "历史是否已验证最佳着法并完成结果文件？仅有猜测不算验证，"
                        "返回 best_move_verified 布尔值。",
                        {"best_move_verified": False},
                    ),
                    probe(
                        "最后一组读取的两个放大顶部渲染文件是什么？返回 paths，按名称排序。",
                        {"paths": ["sqs/top_e1.txt", "sqs/top_e2.txt"]},
                    ),
                    probe(
                        "返回该证据的 position 整数和 nonempty 布尔值。",
                        {"position": 93, "nonempty": True},
                        read_position=93,
                    ),
                ]
            else:
                name = "sympy-in-progress"
                probes = [
                    probe(
                        "按用户期望，空 args 是否足以定义 leaf，必须属于 Atom 子类吗？"
                        "返回 empty_args_is_leaf、atom_subclass_required。",
                        {"empty_args_is_leaf": True, "atom_subclass_required": False},
                        read_position=1,
                    ),
                    probe(
                        "原用户是否允许访问网络和创建 Git commit？"
                        "返回 network_allowed、commit_allowed。",
                        {"network_allowed": False, "commit_allowed": False},
                    ),
                    probe(
                        "atoms 实现所在的项目相对路径是什么？返回 implementation_path。",
                        {"implementation_path": "sympy/core/basic.py"},
                    ),
                    probe(
                        "最后观察到 test_basic 中失败的测试是什么，其原断言期望什么？"
                        "返回 failing_test（模块短名.函数名）、"
                        "old_expected（Python 表达式字符串）。",
                        {"failing_test": "test_basic.test_atoms", "old_expected": "set()"},
                    ),
                    probe(
                        "对于 b1=Basic(), b2=Basic(b1), b21=Basic(b2,b1)，"
                        "按新定义 b21.atoms() 中的叶子变量是谁？返回 leaf_variable。",
                        {"leaf_variable": "b1"},
                    ),
                    probe(
                        "最后工具输出中 test_containers 实际运行多少测试、多少失败？"
                        "返回 tests_run、failures 整数。",
                        {"tests_run": 13, "failures": 0},
                    ),
                    probe(
                        "最后一次跨套件验证是否全部完成？返回 all_suites_verified 布尔值"
                        "及阻断的 runner_error 异常类名。",
                        {"all_suites_verified": False, "runner_error": "UnboundLocalError"},
                    ),
                    probe(
                        "读取最后原始结果，返回 position 和其中 runner_error 异常类名。",
                        {"position": 143, "runner_error": "UnboundLocalError"},
                        read_position=143,
                    ),
                ]
            result.append(
                Checkpoint(
                    name,
                    "historical-checkpoint",
                    messages,
                    probes,
                    {
                        "task": run["task"],
                        "state_path": run["state_path"],
                        "state_sha256": before,
                        "through_position": cp["covered_end_position"],
                        "workspace": "reconstructed evidence; not original task container",
                        "limitations": (
                            "Original transcript and native reasoning preserved; "
                            "controlled questions, not original-task completion."
                        ),
                    },
                )
            )
    if len(result) != 2:
        raise ValueError("Expected both original historical compression checkpoints")
    return result


def build_checkpoints(root: Path) -> list[Checkpoint]:
    return [
        synthetic_checkpoint("short", 5, 15),
        synthetic_checkpoint("stage-boundary", 10, 120),
        *historical_checkpoints(root),
        synthetic_checkpoint("near-budget", 25, 185),
        synthetic_checkpoint("user-correction", 15, 110, correction=True),
    ]


def score_answer(text: str, expected: dict) -> dict:
    raw = text.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.splitlines()[1:-1])
    try:
        answer = json.loads(raw)
    except ValueError:
        return {"format_ok": False, "passed": False, "fields": {key: False for key in expected}}
    if not isinstance(answer, dict):
        return {"format_ok": False, "passed": False, "fields": {key: False for key in expected}}
    fields = {
        key: type(answer.get(key)) is type(value) and answer.get(key) == value
        for key, value in expected.items()
    }
    exact_fields = {
        key: type(answer.get(key)) is type(value) and answer.get(key) == value
        for key, value in expected.items()
    }
    # Freeze these narrow semantic aliases before live evaluation. Do not relax
    # the rubric after observing candidate outputs.
    failure = answer.get("failure")
    if (
        expected.get("failure") == "memory_limit"
        and isinstance(failure, str)
        and failure
        in {
            "out_of_memory",
            "memory_limit_exceeded",
            "oom",
            "memory_exceeded",
        }
    ):
        answer["failure"] = "memory_limit"
    if isinstance(answer.get("failing_test"), str):
        answer["failing_test"] = answer["failing_test"].removeprefix("sympy.core.tests.")
    return {
        "format_ok": True,
        "passed": all(fields.values()),
        "fields": fields,
        "exact_fields": exact_fields,
    }

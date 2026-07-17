from __future__ import annotations

from typing import Any

from bot.tools.base import ToolAnnotations, ToolContext, resolve_path
from bot.tools.subprocess_adapter import SubprocessCliTool


class KsysTool(SubprocessCliTool):
    """Structured adapter for the Kunpeng Performance Boundary Analyzer CLI."""

    name = "ksys"
    description = (
        "调用 KSYS 做性能数据采集、报告、对比或环境稳定性检查。"
        "若当前机器未安装 KSYS，会返回可在目标机器手动运行的命令。"
    )
    executable = "ksys"
    annotations = ToolAnnotations(
        read_only=False,
        destructive=False,
        idempotent=False,
        default_timeout=3600,
        output_limit=2_000_000,
    )
    input_schema = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["collect", "report", "diff", "stability-check"],
            },
            "duration": {"type": "integer", "minimum": 1, "maximum": 86400},
            "interval": {"type": "integer", "minimum": 1},
            "pid": {"type": "integer", "minimum": 1},
            "config_path": {"type": "string"},
            "input_paths": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 2,
            },
            "output_path": {"type": "string"},
            "log_level": {"type": "integer", "minimum": 0, "maximum": 3},
            "workload": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
            },
            "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 86400},
        },
        "required": ["operation"],
        "additionalProperties": False,
    }

    def build_argv(self, context: ToolContext, arguments: dict[str, Any]) -> list[str]:
        operation = str(arguments["operation"])
        argv = [self.executable, operation]
        inputs = [str(item) for item in arguments.get("input_paths", [])]
        workload = [str(item) for item in arguments.get("workload", [])]

        if operation == "report":
            if len(inputs) != 1:
                raise ValueError("KSYS report 需要且只接受一个 input_paths")
            argv.extend(["-i", str(resolve_path(context, inputs[0], must_exist=True))])
        elif operation == "diff":
            if len(inputs) != 2:
                raise ValueError("KSYS diff 需要两个 input_paths")
            argv.extend(
                [
                    "-i",
                    str(resolve_path(context, inputs[0], must_exist=True)),
                    str(resolve_path(context, inputs[1], must_exist=True)),
                ]
            )
        elif inputs:
            raise ValueError(f"KSYS {operation} 不接受 input_paths")

        duration = arguments.get("duration")
        if duration is not None:
            duration = int(duration)
            if operation == "stability-check" and not 10 <= duration <= 120:
                raise ValueError("KSYS stability-check duration 必须在 10-120 秒之间")
            argv.extend(["-d", str(duration)])
        interval = arguments.get("interval")
        if interval is not None:
            argv.extend(["-i", str(int(interval))])
        if arguments.get("pid") is not None:
            if operation != "collect":
                raise ValueError("pid 只适用于 KSYS collect")
            if workload:
                raise ValueError("KSYS collect 的 pid 与 workload 不能同时使用")
            argv.extend(["-p", str(int(arguments["pid"]))])
        if arguments.get("config_path"):
            if operation != "collect":
                raise ValueError("config_path 只适用于 KSYS collect")
            config_path = resolve_path(context, str(arguments["config_path"]), must_exist=True)
            argv.extend(["-c", str(config_path)])
        if arguments.get("output_path"):
            output_path = resolve_path(context, str(arguments["output_path"]), must_exist=False)
            argv.extend(["-o", str(output_path)])
        if arguments.get("log_level") is not None:
            argv.extend(["-l", str(int(arguments["log_level"]))])
        if workload:
            if operation != "collect":
                raise ValueError("workload 只适用于 KSYS collect")
            argv.extend(workload)
        return argv

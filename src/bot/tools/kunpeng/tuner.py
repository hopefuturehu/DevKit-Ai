from __future__ import annotations

from typing import Any

from bot.tools.base import ToolAnnotations, ToolContext, resolve_path
from bot.tools.subprocess_adapter import SubprocessCliTool


class TunerTool(SubprocessCliTool):
    """Structured adapter for `devkit tuner` scenario analysis tasks."""

    name = "tuner"
    description = (
        "调用鲲鹏 DevKit Tuner 执行微架构、热点、Miss、NUMA、HPC 或 Roofline 深度分析。"
        "Tuner 需要鲲鹏 ARM 环境；当前机器不满足时返回手动执行命令。"
    )
    executable = "devkit"
    required_operating_systems = {"linux"}
    required_architectures = {"aarch64", "arm64"}
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
            "task": {
                "type": "string",
                "enum": ["top-down", "hotspot", "miss", "numafast", "hpc-perf", "roofline"],
            },
            "cpu": {"type": "string", "pattern": "^[0-9,-]+$"},
            "duration": {"type": "integer", "minimum": 1, "maximum": 86400},
            "delay": {"type": "integer", "minimum": 0, "maximum": 86400},
            "interval": {"type": "integer", "minimum": 1, "maximum": 86400},
            "pid": {"type": "string", "pattern": "^(ALL|[0-9,]+)$"},
            "mode": {"type": "string", "enum": ["user", "kernel", "all"]},
            "cgroup": {"type": "string"},
            "log_level": {"type": "integer", "minimum": 0, "maximum": 3},
            "topdown_level": {"type": "integer", "minimum": 0, "maximum": 6},
            "event": {"type": "string"},
            "call_graph": {"type": "boolean", "default": False},
            "package": {"type": "boolean", "default": False},
            "long_name": {"type": "boolean", "default": False},
            "dwarf": {"type": "boolean", "default": False},
            "src_dir": {"type": "string"},
            "output_path": {"type": "string"},
            "roofline_mode": {"type": "string", "enum": ["total", "region"]},
            "hbm_mode": {"type": "string", "enum": ["cache", "flat"]},
            "workload": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
            },
            "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 86400},
        },
        "required": ["task"],
        "additionalProperties": False,
    }

    def build_argv(self, context: ToolContext, arguments: dict[str, Any]) -> list[str]:
        task = str(arguments["task"])
        argv = [self.executable, "tuner", task]
        workload = [str(item) for item in arguments.get("workload", [])]
        selectors = [
            bool(arguments.get("cpu")),
            bool(arguments.get("pid")),
            bool(arguments.get("cgroup")),
            bool(workload),
        ]
        if sum(selectors) > 1:
            raise ValueError("cpu、pid、cgroup 和 workload 最多指定一种")

        common_flags = {
            "cpu": "-c",
            "duration": "-d",
            "delay": "-D",
            "interval": "-i",
            "pid": "-p",
            "mode": "-r",
            "cgroup": "-G",
            "log_level": "-l",
        }
        for key, flag in common_flags.items():
            value = arguments.get(key)
            if value is not None:
                argv.extend([flag, str(value)])

        if arguments.get("topdown_level") is not None:
            if task != "top-down":
                raise ValueError("topdown_level 只适用于 top-down")
            argv.extend(["-L", str(int(arguments["topdown_level"]))])
        if arguments.get("event"):
            if task != "hotspot":
                raise ValueError("event 只适用于 hotspot")
            argv.extend(["-e", str(arguments["event"])])
        if arguments.get("call_graph"):
            if task != "hotspot":
                raise ValueError("call_graph 只适用于 hotspot")
            argv.append("-g")
        if arguments.get("package"):
            argv.append("--package")
        if arguments.get("long_name"):
            if task != "hotspot":
                raise ValueError("long_name 只适用于 hotspot")
            argv.append("--long-name")
        if arguments.get("dwarf"):
            if task != "hotspot":
                raise ValueError("dwarf 只适用于 hotspot")
            argv.append("--dwarf")
        if arguments.get("src_dir"):
            if task != "hotspot":
                raise ValueError("src_dir 只适用于 hotspot")
            src_dir = resolve_path(context, str(arguments["src_dir"]), must_exist=True)
            argv.extend(["-s", str(src_dir)])
        if arguments.get("output_path"):
            output_path = resolve_path(context, str(arguments["output_path"]), must_exist=False)
            argv.extend(["-o", str(output_path)])
        if arguments.get("roofline_mode"):
            if task != "roofline":
                raise ValueError("roofline_mode 只适用于 roofline")
            argv.extend(["-m", str(arguments["roofline_mode"])])
        if arguments.get("hbm_mode"):
            if task != "roofline":
                raise ValueError("hbm_mode 只适用于 roofline")
            argv.extend(["--hbm-mode", str(arguments["hbm_mode"])])
        argv.extend(workload)
        return argv

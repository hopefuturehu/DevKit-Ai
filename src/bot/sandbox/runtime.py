"""Read-only runtime discovery; installation facts never imply OS enforcement.

No executable is selected from project configuration or the inherited PATH.
Only a trusted host caller may supply an alternate installation root. The
execution backend must additionally run its real isolation preflight probes.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
from dataclasses import dataclass
from pathlib import Path

from bot.sandbox.models import SandboxCapabilities

MANIFEST_NAMES = ("package.json", "package-lock.json", "runtime-lock.json")


def manifest_directory() -> Path:
    packaged = Path(__file__).resolve().parents[1] / "assets/sandbox"
    if packaged.is_dir():
        return packaged
    # Editable source checkout; normal wheels contain the same pinned files.
    return Path(__file__).resolve().parents[3] / "runtime/sandbox"


def _read(path: Path, limit: int = 2_000_000) -> bytes:
    with path.open("rb") as handle:
        content = handle.read(limit + 1)
    if len(content) > limit:
        raise ValueError(f"运行时清单超过大小限制: {path.name}")
    return content


@dataclass(frozen=True)
class RuntimeInspection:
    root: Path
    capabilities: SandboxCapabilities
    executables: tuple[tuple[str, Path], ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def metadata_valid(self) -> bool:
        return not self.errors


def inspect_runtime(workspace: Path, *, root: Path | None = None) -> RuntimeInspection:
    root = (root or Path("~/.bot/sandbox/runtime")).expanduser().resolve()
    workspace = workspace.resolve()
    errors: list[str] = []
    executables: dict[str, Path] = {}
    node_version = srt_version = None
    try:
        if platform.system() not in {"Darwin", "Linux"}:
            raise ValueError("原生沙箱首版只支持 macOS/Linux")
        if root.is_relative_to(workspace) or workspace.is_relative_to(root):
            raise ValueError("沙箱运行时必须安装于工作区之外的独立目录")
        info = root.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
            raise ValueError("运行时目录不能由组或其他用户写入")
        if info.st_uid not in {0, os.getuid()}:
            raise ValueError("运行时目录所有者无效")
        source = manifest_directory()
        pinned = {name: _read(source / name) for name in MANIFEST_NAMES}
        lock = json.loads(pinned["runtime-lock.json"])
        architecture = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64"}.get(
            platform.machine().lower(), "unsupported"
        )
        target = f"{platform.system().lower()}-{architecture}"
        if target not in lock["node_archives"]:
            raise ValueError(f"锁定运行时不支持当前平台: {target}")

        def installed(name: str) -> bytes:
            path = root / name
            if not path.resolve().is_relative_to(root):
                raise ValueError(f"安装文件逃逸运行时目录: {name}")
            return _read(path)

        record = json.loads(installed("installation.json"))
        if record["platform"] != target or record["node"] != "v" + lock["node_version"]:
            raise ValueError("安装记录的平台或 Node 版本与锁定清单不符")
        for name, content in pinned.items():
            if (
                installed(name) != content
                or record["source_hashes"][name] != hashlib.sha256(content).hexdigest()
            ):
                raise ValueError(f"安装清单与当前锁定版本不符: {name}")
        package = json.loads(installed("node_modules/@anthropic-ai/sandbox-runtime/package.json"))
        if package["version"] != lock["srt_version"]:
            raise ValueError("SRT 安装版本与锁定清单不符")
        for name, relative in (
            ("node", "node/bin/node"),
            ("srt", "node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js"),
        ):
            path = (root / relative).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError(f"缺少可信 {name} 启动文件")
            if name == "node" and not os.access(path, os.X_OK):
                raise ValueError("Node 启动文件不可执行")
            executables[name] = path
        node_version, srt_version = lock["node_version"], lock["srt_version"]
        dependencies = (
            ("sandbox-exec",) if platform.system() == "Darwin" else ("bwrap", "socat", "rg")
        )
        for name in dependencies:
            # These are host installation locations, never the caller's PATH.
            path = next(
                (
                    (Path(directory) / name).resolve()
                    for directory in ("/usr/bin", "/bin", "/opt/homebrew/bin", "/usr/local/bin")
                    if (Path(directory) / name).is_file()
                    and os.access(Path(directory) / name, os.X_OK)
                ),
                None,
            )
            if path is None or path.is_relative_to(workspace):
                errors.append(f"缺少工作区外的系统依赖: {name}")
            else:
                executables[name] = path
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(f"运行时元数据检查失败: {exc}")
    status = "unavailable" if errors else "unverified"
    capabilities = SandboxCapabilities(
        backend_version=srt_version,
        node_version=node_version,
        # platform.platform() can invoke `uname` via the inherited PATH.
        # system()/release() use cached uname fields without launching tools.
        os_version=f"{platform.system()} {platform.release()}",
        architecture=platform.machine(),
        file_read=status,
        file_write=status,
        network_filter=status,
        process_isolation=status,
        evidence=("metadata inspection only; isolation probes not run",),
    )
    return RuntimeInspection(root, capabilities, tuple(sorted(executables.items())), tuple(errors))

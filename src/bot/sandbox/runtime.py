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

from bot.sandbox.host_runtime import (
    MANIFEST_LIMIT as HOST_MANIFEST_LIMIT,
)
from bot.sandbox.host_runtime import (
    MANIFEST_NAME as HOST_MANIFEST_NAME,
)
from bot.sandbox.host_runtime import (
    HostExecutables,
    inspect_host_executables,
)
from bot.sandbox.loader_controls import (
    MANIFEST_LIMIT as LOADER_MANIFEST_LIMIT,
)
from bot.sandbox.loader_controls import (
    MANIFEST_NAME as LOADER_MANIFEST_NAME,
)
from bot.sandbox.loader_controls import (
    LoaderControls,
    inspect_loader_controls,
)
from bot.sandbox.models import SandboxCapabilities
from bot.sandbox.runtime_integrity import RuntimeTree, inspect_tree, read_installed
from bot.sandbox.worker_runtime import WorkerRuntime, inspect_worker

MANIFEST_NAMES = ("package.json", "package-lock.json", "runtime-lock.json")
RUNTIME_ASSET_NAMES = (*MANIFEST_NAMES, "launcher.mjs", "bwrap-launcher.sh", "worker.py")


def runtime_asset(name: str) -> Path:
    return (
        Path(__file__).with_name("worker.py")
        if name == "worker.py"
        else manifest_directory() / name
    )


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
    worker: WorkerRuntime | None = None
    tree: RuntimeTree | None = None
    host: HostExecutables | None = None
    loader: LoaderControls | None = None

    @property
    def metadata_valid(self) -> bool:
        return not self.errors


def inspect_runtime(
    workspace: Path,
    *,
    root: Path | None = None,
    worker_snapshot: WorkerRuntime | None = None,
    tree_snapshot: RuntimeTree | None = None,
    host_snapshot: HostExecutables | None = None,
    loader_snapshot: LoaderControls | None = None,
) -> RuntimeInspection:
    root = (root or Path("~/.bot/sandbox/runtime")).expanduser().resolve()
    workspace = workspace.resolve()
    errors: list[str] = []
    executables: dict[str, Path] = {}
    node_version = srt_version = None
    worker = None
    tree = None
    host = None
    loader = None
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
        pinned = {name: _read(runtime_asset(name)) for name in RUNTIME_ASSET_NAMES}
        lock = json.loads(pinned["runtime-lock.json"])
        architecture = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64"}.get(
            platform.machine().lower(), "unsupported"
        )
        target = f"{platform.system().lower()}-{architecture}"
        if target not in lock["node_archives"]:
            raise ValueError(f"锁定运行时不支持当前平台: {target}")

        def installed(name: str) -> bytes:
            return read_installed(root, name, limit=2_000_000)

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
            ("launcher", "launcher.mjs"),
        ):
            path = (root / relative).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError(f"缺少可信 {name} 启动文件")
            if name == "node" and not os.access(path, os.X_OK):
                raise ValueError("Node 启动文件不可执行")
            executables[name] = path
        if platform.system() == "Linux" and not os.access(root / "bwrap-launcher.sh", os.X_OK):
            raise ValueError("bubblewrap 启动器不可执行")
        node_version, srt_version = lock["node_version"], lock["srt_version"]
        tree = inspect_tree(
            root,
            workspace,
            manifest_sha256=record["runtime_files_sha256"],
            previous=tree_snapshot,
        )
        worker = inspect_worker(
            root,
            workspace,
            manifest_sha256=record["worker_python_sha256"],
            previous=worker_snapshot,
        )
        host = inspect_host_executables(
            read_installed(root, HOST_MANIFEST_NAME, limit=HOST_MANIFEST_LIMIT),
            manifest_sha256=record["host_executables_sha256"],
            workspace=workspace,
            previous=host_snapshot,
        )
        executables.update((name, state.canonical) for name, state in host.executables)
        loader = inspect_loader_controls(
            read_installed(root, LOADER_MANIFEST_NAME, limit=LOADER_MANIFEST_LIMIT),
            manifest_sha256=record["loader_controls_sha256"],
            workspace=workspace,
            previous=loader_snapshot,
        )
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
    return RuntimeInspection(
        root,
        capabilities,
        tuple(sorted(executables.items())),
        tuple(errors),
        worker,
        tree,
        host,
        loader,
    )

#!/usr/bin/env python3
"""Install a pinned SRT + Node runtime outside the task workspace.

This is an explicit, networked installation step, never an execution-time
fallback. npm lifecycle scripts are disabled and all npm tarballs are locked
by package-lock.json. The Node archive is checked before extraction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "runtime" / "sandbox"


def install(destination: Path) -> Path:
    destination = destination.expanduser().resolve()
    workspace = SOURCE.parents[1]
    if destination.is_relative_to(workspace):
        raise ValueError("runtime must be installed outside the task workspace")
    if destination.exists():
        raise ValueError(f"destination already exists (will not overwrite): {destination}")
    lock = json.loads((SOURCE / "runtime-lock.json").read_text())
    architecture = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "x64"}.get(platform.machine())
    target = f"{platform.system().lower()}-{architecture}"
    if target not in lock["node_archives"]:
        raise ValueError(f"unsupported runtime platform: {target}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".bot-runtime-install-", dir=destination.parent
    ) as temp:
        stage = Path(temp)
        artifact = f"node-v{lock['node_version']}-{target}"
        archive = stage / "node.tar.gz"
        url = f"https://nodejs.org/dist/v{lock['node_version']}/{artifact}.tar.gz"
        with urllib.request.urlopen(url, timeout=60) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        with archive.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if digest != lock["node_archives"][target]:
            raise ValueError("Node archive checksum mismatch")
        with tarfile.open(archive) as package:
            package.extractall(stage, filter="data")
        root = stage / "runtime"
        root.mkdir(mode=0o700)
        (stage / artifact).rename(root / "node")
        for name in ("package.json", "package-lock.json", "runtime-lock.json", "launcher.mjs"):
            shutil.copyfile(SOURCE / name, root / name)
        node = root / "node" / "bin" / "node"
        npm = root / "node" / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
        env = {
            key: value
            for key, value in os.environ.items()
            if key in {"HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "NO_PROXY"}
        }
        npm_home = stage / "npm-home"
        npm_home.mkdir()
        user_config = npm_home / "user.npmrc"
        global_config = npm_home / "global.npmrc"
        user_config.touch()
        global_config.touch()
        env.update(
            HOME=str(npm_home),
            PATH=f"{node.parent}:/usr/bin:/bin",
            npm_config_cache=str(stage / "npm-cache"),
            npm_config_userconfig=str(user_config),
            npm_config_globalconfig=str(global_config),
        )
        subprocess.run(
            [
                str(node),
                str(npm),
                "ci",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "--fetch-retries=0",
                "--fetch-timeout=30000",
            ],
            cwd=root,
            env=env,
            timeout=180,
            check=True,
        )
        package = root / "node_modules" / "@anthropic-ai" / "sandbox-runtime" / "package.json"
        if json.loads(package.read_text())["version"] != lock["srt_version"]:
            raise ValueError("installed SRT version does not match lock")
        version = subprocess.check_output([str(node), "--version"], text=True, env=env).strip()
        if version != f"v{lock['node_version']}":
            raise ValueError("installed Node version does not match lock")
        hashes = {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in ("package.json", "package-lock.json", "runtime-lock.json", "launcher.mjs")
        }
        (root / "installation.json").write_text(
            json.dumps({"platform": target, "source_hashes": hashes, "node": version}, indent=2)
            + "\n"
        )
        root.rename(destination)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    print(install(args.destination))


if __name__ == "__main__":
    main()

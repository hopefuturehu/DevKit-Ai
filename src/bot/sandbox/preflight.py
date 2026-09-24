"""One-shot live isolation checks using only host-created disposable resources.

Only this fixed, trusted probe's output is decoded. Ordinary command output
cannot attest capabilities. A failed/cancelled check is never silently retried.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from bot.config.models import AppConfig
from bot.execution.base import ProcessSpec, ProcessStatus
from bot.execution.local import LocalExecutionTarget
from bot.execution.native import NativeSandboxExecutionTarget
from bot.sandbox.models import SandboxCapabilities, SandboxError, SandboxErrorCode
from bot.sandbox.resources import OwnedDirectory
from bot.sandbox.runtime import RuntimeInspection
from bot.sandbox.worker import decode_json_line

_CHECK_JS = r"""
const fs=require('fs'), net=require('net'), dgram=require('dgram'), http=require('http');
const {spawnSync}=require('child_process');
const c=JSON.parse(process.argv[1]);
const codes=['EPERM','EACCES','EROFS','ENOENT','ENOTDIR'];
const denied=f=>{try{f();return false}catch(e){return codes.includes(e.code)}};
const checks={read:fs.readFileSync('input','utf8')===c.nonce};
fs.writeFileSync('output',c.nonce);checks.write=true;
checks.outside_read=denied(()=>fs.readFileSync(c.outside));
checks.secret_read=denied(()=>fs.readFileSync('.env'));
checks.outside_write=denied(()=>fs.writeFileSync(c.outside,'modified'));
const child=spawnSync(process.execPath,['-e',
  `const fs=require('fs');let denied=false;
   try{fs.writeFileSync(process.argv[1],'child-modified')}
   catch(e){denied=['EPERM','EACCES','EROFS','ENOENT','ENOTDIR'].includes(e.code)};
   console.log(JSON.stringify({denied}));`,c.outside],
  {encoding:'utf8',timeout:2000});
checks.child_write=child.status===0 && JSON.parse(child.stdout).denied===true;
const stream=endpoint=>new Promise(resolve=>{
  const s=net.createConnection(endpoint);let settled=false;
  const finish=blocked=>{if(settled)return;settled=true;s.destroy();resolve(blocked)};
  s.setTimeout(900,()=>finish(true));s.on('error',()=>finish(true));
  s.on('connect',()=>{s.write(c.nonce+':sandbox');finish(false)});
});
const datagram=(endpoint,type)=>new Promise(resolve=>{
  const s=dgram.createSocket(type);let settled=false;
  const finish=blocked=>{if(settled)return;settled=true;clearTimeout(timer);
    try{s.close()}catch{};resolve(blocked)};
  const timer=setTimeout(()=>finish(true),900);
  s.on('error',()=>finish(true));s.on('message',()=>finish(false));
  s.send(c.nonce+':sandbox',endpoint.port,endpoint.host,e=>{if(e)finish(true)});
});
const proxy=()=>new Promise(resolve=>{
  if(!process.env.HTTP_PROXY)return resolve(false);
  const p=new URL(process.env.HTTP_PROXY), headers={};
  if(p.username)headers['Proxy-Authorization']='Basic '+Buffer.from(
    decodeURIComponent(p.username)+':'+decodeURIComponent(p.password)).toString('base64');
  const r=http.get({host:p.hostname,port:p.port,path:`http://${c.tcp4.host}:${c.tcp4.port}/`,headers},res=>{
    res.resume();resolve(res.statusCode===403);
  });
  r.setTimeout(1500,()=>{r.destroy();resolve(false)});r.on('error',()=>resolve(false));
});
Promise.all([stream(c.tcp4),stream(c.tcp6),stream({path:c.unix}),
  datagram(c.udp4,'udp4'),datagram(c.udp6,'udp6'),proxy()]).then(results=>{
  for(const [i,key] of ['tcp4','tcp6','unix','udp4','udp6','proxy'].entries()){
    checks[key]=results[i];
  }
  console.log(JSON.stringify({v:1,nonce:c.nonce,checks,argv:process.argv.slice(2),home:process.env.HOME}));
}).catch(()=>process.exitCode=1);
"""

_CANCEL_JS = r"""
const fs=require('fs');const c=JSON.parse(process.argv[1]);
let readonly=false;try{fs.writeFileSync('readonly-marker','bad')}
catch(e){readonly=['EPERM','EACCES','EROFS'].includes(e.code)};
fs.writeFileSync(process.env.TMPDIR+'/private',c.nonce);
console.log(JSON.stringify({v:1,nonce:c.nonce,readonly}));setInterval(()=>{},1000);
"""

CHECKS = frozenset(
    {
        "read",
        "write",
        "outside_read",
        "secret_read",
        "outside_write",
        "child_write",
        "tcp4",
        "tcp6",
        "unix",
        "udp4",
        "udp6",
        "proxy",
    }
)
ARGV = ("", "中文", "a'b", "line\nbreak")


async def _settle(task: asyncio.Task) -> None:
    """Finish cleanup despite repeated cancellation of the waiting caller."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break


class StartupGate:
    def __init__(self, factory):
        self.factory = factory
        self.instance = None
        self.task: asyncio.Task | None = None
        self.closed = False

    @property
    def live(self) -> bool:
        return bool(
            (self.task is not None and not self.task.done())
            or (self.instance is not None and self.instance.has_live_processes)
        )

    def _cancel_probe(self):
        # Other waiters also receive cancellation of this shared task. They
        # must not inject another cancellation into its cleanup path.
        if self.task is not None and not self.task.done() and not self.task.cancelling():
            self.task.cancel()

    @staticmethod
    def _failure(exc):
        code = (
            SandboxErrorCode.CLEANUP_FAILED
            if isinstance(exc, SandboxError) and exc.code == SandboxErrorCode.CLEANUP_FAILED
            else SandboxErrorCode.UNAVAILABLE
        )
        return SandboxError(
            code,
            f"沙箱自检失败，业务目标未启动: {str(exc)[:2000]}",
            stage="isolation_preflight",
            executed=False,
        )

    async def ensure(self):
        if self.closed:
            raise SandboxError(SandboxErrorCode.UNAVAILABLE, "沙箱自检已关闭")
        # Assignment precedes the first await: concurrent callers share one task.
        if self.task is None:
            self.instance = self.factory()
            self.task = asyncio.create_task(self.instance.run())
        try:
            return await asyncio.shield(self.task)
        except asyncio.CancelledError:
            self._cancel_probe()
            await _settle(self.task)
            if not self.task.cancelled():
                failure = self.task.exception()
                if (
                    isinstance(failure, SandboxError)
                    and failure.code == SandboxErrorCode.CLEANUP_FAILED
                ):
                    raise self._failure(failure) from failure
            raise
        except Exception as exc:
            raise self._failure(exc) from exc

    async def aclose(self):
        self.closed = True
        if self.task is not None:
            self._cancel_probe()
            await _settle(self.task)
            if not self.task.cancelled():
                self.task.exception()
        if self.instance is not None:
            await self.instance.aclose()


class _ProbeTarget(NativeSandboxExecutionTarget):
    """Private bootstrap: only fixed probe code may use this entrypoint."""

    async def start_process(self, spec):
        return await LocalExecutionTarget.start_process(self, spec)

    def _discover_preflights(self):
        if any(self.state_root.glob("preflight-*")):
            raise ValueError("固定探针不能拥有递归自检资源")


class _Datagrams(asyncio.DatagramProtocol):
    def __init__(self):
        self.received = []
        self.closed = asyncio.get_running_loop().create_future()

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.received.append(data)
        self.transport.sendto(data, addr)

    def connection_lost(self, exc):
        if not self.closed.done():
            self.closed.set_result(None)


@dataclass(frozen=True)
class PreflightReport:
    probe_id: str
    evidence_sha256: str
    capabilities: SandboxCapabilities


class NativePreflight:
    def __init__(self, runtime: RuntimeInspection, state_root: Path, cleanup, *, parent=None):
        self.runtime = runtime
        self.state_root = state_root
        self.cleanup = cleanup
        self.parent = parent
        self.resources = None
        self.manager = None
        self.owned = None
        self.socket_directory = None
        self.servers = []
        self.datagrams = []
        self.handlers = set()
        self.received = []
        self.endpoints = {}
        self.nonce = uuid4().hex

    @property
    def has_live_processes(self):
        return self.manager is not None and self.manager.has_live_processes

    async def _stream(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        try:
            async with asyncio.timeout(2):
                data = await reader.readexactly(len(self.nonce) + len(":control"))
                self.received.append(data)
                writer.write(data)
                await writer.drain()
        except asyncio.IncompleteReadError as exc:
            self.received.append(exc.partial)
        except (ConnectionError, TimeoutError):
            self.received.append(b"incomplete")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            finally:
                self.handlers.discard(task)

    async def _listeners(self):
        for key, host, family in (
            ("tcp4", "127.0.0.1", socket.AF_INET),
            ("tcp6", "::1", socket.AF_INET6),
        ):
            server = await asyncio.start_server(self._stream, host, 0, family=family)
            self.servers.append(server)
            self.endpoints[key] = {"host": host, "port": server.sockets[0].getsockname()[1]}
        self.socket_directory = OwnedDirectory.create(Path("/tmp").resolve(), prefix="bot-check-")
        path = self.socket_directory.path / "socket"
        self.servers.append(await asyncio.start_unix_server(self._stream, str(path)))
        self.endpoints["unix"] = str(path)
        loop = asyncio.get_running_loop()
        for key, host, family in (
            ("udp4", "127.0.0.1", socket.AF_INET),
            ("udp6", "::1", socket.AF_INET6),
        ):
            transport, protocol = await loop.create_datagram_endpoint(
                _Datagrams, local_addr=(host, 0), family=family
            )
            self.datagrams.append((transport, protocol))
            self.endpoints[key] = {"host": host, "port": transport.get_extra_info("sockname")[1]}

    async def _positive_controls(self):
        message = (self.nonce + ":control").encode()
        async with asyncio.timeout(5):
            for key in ("tcp4", "tcp6", "unix"):
                if key == "unix":
                    reader, writer = await asyncio.open_unix_connection(self.endpoints[key])
                else:
                    endpoint = self.endpoints[key]
                    reader, writer = await asyncio.open_connection(
                        endpoint["host"], endpoint["port"]
                    )
                try:
                    writer.write(message)
                    await writer.drain()
                    if await reader.readexactly(len(message)) != message:
                        raise ValueError("自检 TCP/Unix 端点正向对照失败")
                finally:
                    writer.close()
                    await writer.wait_closed()
            for key, family in (("udp4", socket.AF_INET), ("udp6", socket.AF_INET6)):
                endpoint = self.endpoints[key]
                with socket.socket(family, socket.SOCK_DGRAM) as sock:
                    sock.setblocking(False)
                    loop = asyncio.get_running_loop()
                    await loop.sock_sendto(sock, message, (endpoint["host"], endpoint["port"]))
                    data, _ = await loop.sock_recvfrom(sock, 128)
                    if data != message:
                        raise ValueError("自检 UDP 端点正向对照失败")

    def _spec(self, script, arguments, *, readonly=False):
        scope = self.manager.bind_scope(
            session_id=self.nonce,
            run_id="readonly" if readonly else "normal",
            tool_call_id=uuid4().hex,
            read_only=readonly,
        )
        return ProcessSpec(
            argv=[str(dict(self.runtime.executables)["node"]), "-e", script, *arguments],
            cwd=self.manager.workspace,
            session_id=scope.session_id,
            run_id=scope.run_id,
            scope=scope,
            timeout_seconds=10,
            output_limit_bytes=20_000,
        )

    async def run(self):
        try:
            if any(self.state_root.glob("preflight-*")):
                raise ValueError("存在未解决的自检遗留目录；拒绝重新启动探针或业务命令")
            self.owned = OwnedDirectory.create(self.state_root, prefix="preflight-")
            work = self.owned.path / "work"
            work.mkdir(mode=0o700)
            (work / "input").write_text(self.nonce)
            (work / ".env").write_text(self.nonce + "-secret")
            outside = self.owned.path / "outside"
            outside.write_text(self.nonce + "-outside")
            config = AppConfig.model_validate({"sandbox": {"mode": "required"}})
            config.agent.process_cleanup = self.cleanup.model_copy(deep=True)
            self.manager = _ProbeTarget(
                config,
                workspace=work,
                runtime_root=self.runtime.root,
                state_root=self.owned.path / "runs",
            )
            await self._listeners()
            from bot.sandbox.preflight_recovery import ProbeResources

            self.resources = ProbeResources.create(
                self.parent, self.owned, self.socket_directory, self.manager
            )
            await self._positive_controls()
            args = {**self.endpoints, "nonce": self.nonce, "outside": str(outside)}
            spec = self._spec(_CHECK_JS, [json.dumps(args), *ARGV])
            pid = await self.manager.start_process(spec)
            result = await self.manager.poll_process(pid, wait_seconds=15, consume_output=False)
            if (
                result.status != ProcessStatus.COMPLETED
                or result.returncode != 0
                or not result.output_complete
                or result.truncated
            ):
                raise ValueError(
                    f"隔离探针未正常完成: {result.status.value}; {result.stderr[:1000]}"
                )
            reply = decode_json_line(result.stdout.encode(), limit=20_000)
            if (
                reply.keys() != {"v", "nonce", "checks", "argv", "home"}
                or type(reply["v"]) is not int
                or reply["v"] != 1
                or reply["nonce"] != self.nonce
                or reply["argv"] != list(ARGV)
                or reply["home"]
                != str(self.manager._runs[(self.nonce, "normal")].directory.path / "home")
                or not isinstance(reply["checks"], dict)
                or reply["checks"].keys() != CHECKS
                or not all(value is True for value in reply["checks"].values())
                or outside.read_text() != self.nonce + "-outside"
                or (work / ".env").read_text() != self.nonce + "-secret"
                or (work / "output").read_text() != self.nonce
            ):
                checks = reply.get("checks")
                failed = sorted(
                    key
                    for key in CHECKS
                    if not isinstance(checks, dict) or checks.get(key) is not True
                )
                raise ValueError(
                    "文件/网络/子进程/argv 隔离自检失败: "
                    + (", ".join(failed) or "协议或宿主对照不匹配")
                )
            await self._positive_controls()
            expected = (self.nonce + ":control").encode()
            if any(data != expected for data in self.received) or any(
                data != expected for _, protocol in self.datagrams for data in protocol.received
            ):
                raise ValueError("受限进程到达了宿主自检端点")
            cancellation = self._spec(
                _CANCEL_JS, [json.dumps({"nonce": self.nonce})], readonly=True
            )
            pid = await self.manager.start_process(cancellation)
            async with asyncio.timeout(5):
                while True:
                    snapshot = await self.manager.poll_process(
                        pid, wait_seconds=0.05, consume_output=False
                    )
                    if snapshot.stdout.endswith("\n"):
                        break
                    if snapshot.status != ProcessStatus.RUNNING:
                        raise ValueError("取消探针未就绪")
            ready = decode_json_line(snapshot.stdout.encode(), limit=20_000)
            if (
                ready != {"v": 1, "nonce": self.nonce, "readonly": True}
                or type(ready["v"]) is not int
                or ready["readonly"] is not True
                or (work / "readonly-marker").exists()
            ):
                raise ValueError("只读自检失败")
            cancelled = await self.manager.terminate_process(pid, reason="启动自检取消验证")
            if (
                cancelled.status != ProcessStatus.CANCELLED
                or cancelled.cleanup_status != "observed_empty"
            ):
                raise ValueError("自检进程无法确认回收")
            evidence = hashlib.sha256(json.dumps(reply, sort_keys=True).encode()).hexdigest()
            capabilities = self.runtime.capabilities.model_copy(
                update={
                    "file_read": "enforced",
                    "file_write": "enforced",
                    "network_filter": "enforced",
                    "evidence": (
                        "live fixed probe v1: read/write/readonly/child/argv; "
                        "deny TCP4/TCP6/UDP4/UDP6/Unix/proxy",
                        "host endpoints verified before and after; "
                        "managed cancellation observed empty",
                        "process ownership remains observational; "
                        "no complete process containment or resource limits claim",
                        f"probe_id={self.nonce}; evidence_sha256={evidence}",
                    ),
                }
            )
            return PreflightReport(self.nonce, evidence, capabilities)
        finally:
            cleanup = asyncio.create_task(self.aclose())
            await _settle(cleanup)
            cleanup.result()

    async def _close_endpoints(self):
        for server in self.servers:
            server.close()
        await asyncio.gather(*(server.wait_closed() for server in self.servers))
        for transport, _ in self.datagrams:
            transport.close()
        await asyncio.gather(*(p.closed for _, p in self.datagrams))
        for handler in tuple(self.handlers):
            handler.cancel()
        await asyncio.gather(*tuple(self.handlers), return_exceptions=True)

    async def aclose(self):
        failures = []
        try:
            await self._close_endpoints()
        except Exception as exc:
            failures.append(exc)
        if self.manager is not None:
            try:
                await self.manager.aclose()
            except Exception as exc:
                failures.append(exc)
        if not failures:
            try:
                if self.resources is not None:
                    self.resources.remove()
                else:
                    if self.socket_directory is not None:
                        self.socket_directory.remove()
                    if self.owned is not None:
                        self.owned.remove()
            except Exception as exc:
                failures.append(exc)
        if failures:
            error = SandboxError(
                SandboxErrorCode.CLEANUP_FAILED,
                "自检进程或资源清理未完成，保留未回收资源",
                stage="isolation_preflight",
                executed=False,
            )
            # Fixed probe identities only, never business stdout/env/arguments.
            # This is diagnostic evidence, not authority to adopt or kill PIDs.
            processes = []
            if self.manager is not None:
                for managed in self.manager._managed_processes.values():
                    processes.append(
                        {
                            "process_id": managed.process_id,
                            "root_pid": managed.scope.root_pid,
                            "status": managed.status.value,
                            "cleanup_status": managed.cleanup_status,
                            "unknown": managed.scope.unknown,
                            "remaining": managed.scope.remaining()[:16],
                            "unattributed": [
                                asdict(identity)
                                for identity in list(managed.scope.unattributed.values())[:16]
                            ],
                            "unattributed_count": len(managed.scope.unattributed),
                        }
                    )
            error.add_note(
                json.dumps(
                    {
                        "probe_id": self.nonce,
                        "directory": str(self.owned.path) if self.owned is not None else None,
                        "processes": processes,
                    }
                )
            )
            raise error from ExceptionGroup("preflight cleanup failures", failures)

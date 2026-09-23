#!/usr/bin/env python3
"""P0 evidence collector: run the pinned SRT against disposable fixtures.

Run outside an enclosing OS sandbox. A failed probe exits nonzero and writes
its evidence; it never retries the target without SRT. This is not the bot's
execution backend or a claim that the full acceptance matrix has passed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psutil


def clean_environment(runtime: Path, fixture: Path) -> dict[str, str]:
    return {
        "PATH": f"{runtime / 'node/bin'}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(fixture / "home"),
        "TMPDIR": str(fixture / "tmp"),
        "XDG_CACHE_HOME": str(fixture / "cache"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }


def settings(runtime: Path, fixture: Path) -> dict:
    system = ["/usr", "/bin", "/sbin", "/dev"]
    if platform.system() == "Darwin":
        system += ["/System", "/private/etc/ssl", "/private/etc/hosts", "/private/etc/resolv.conf"]
    else:
        system += ["/lib", "/lib64", "/proc", "/etc/ld.so.cache", "/etc/ssl", "/etc/hosts"]
        system += ["/etc/resolv.conf", "/etc/nsswitch.conf", "/etc/passwd", "/etc/group"]
    return {
        "filesystem": {
            "denyRead": ["/", str(fixture / "work/.env"), str(fixture / "work/secret")],
            "allowRead": system
            + [str(runtime)]
            + [str(fixture / name) for name in ("work", "home", "tmp", "cache")],
            "allowWrite": [str(fixture / name) for name in ("work", "home", "tmp", "cache")],
            "denyWrite": [str(fixture / "work/.env"), str(fixture / "work/secret")],
        },
        "network": {
            "allowedDomains": [],
            "deniedDomains": [],
            "allowAllUnixSockets": False,
            "allowLocalBinding": False,
            "deniedResolvedAddresses": [
                "10.0.0.0/8",
                "172.16.0.0/12",
                "192.168.0.0/16",
                "100.64.0.0/10",
                "fc00::/7",
            ],
        },
        "enableWeakerNestedSandbox": False,
        "enableWeakerNetworkIsolation": False,
    }


class Probe:
    def __init__(
        self, runtime: Path, fixture: Path, *, deny_hardlinks: bool = False, helper: bool = False
    ):
        self.runtime = runtime
        self.fixture = fixture
        self.node = runtime / "node/bin/node"
        self.cli = runtime / "node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js"
        self.policy = fixture / "control/policy.json"
        self.env = clean_environment(runtime, fixture)
        self.cases: list[dict] = []
        self.deny_hardlinks = deny_hardlinks
        self.helper = helper

    def argv(self, code: str, *args: str) -> list[str]:
        return [
            str(self.node),
            *(
                [str(self.runtime / "launcher.mjs")]
                if self.helper
                else [str(self.cli), "--settings"]
            ),
            str(self.policy),
            "--",
            str(self.node),
            "--eval",
            code,
            "--",
            *args,
        ]

    def run(self, name: str, code: str, *args: str, input_text: str | None = None) -> dict:
        started = time.monotonic()
        process = subprocess.Popen(
            self.argv(code, *args),
            cwd=self.fixture / "work",
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            close_fds=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(input_text, timeout=25)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate(timeout=5)
        result = {
            "name": name,
            "returncode": process.returncode,
            "duration_seconds": round(time.monotonic() - started, 4),
            "stdout": stdout,
            "stderr": stderr,
            "passed": False,
        }
        self.cases.append(result)
        return result

    def filesystem(self) -> None:
        fixture = self.fixture
        (fixture / "work/normal").write_text("normal-fixture")
        for path in ("work/.env", "work/secret", "outside/secret", "control/state.db"):
            (fixture / path).write_text("synthetic-secret")
        (fixture / "outside/sentinel").write_text("unchanged")
        (fixture / "work/outside-link").symlink_to(fixture / "outside")
        # Existing hard links must be tested separately from symlinks: a
        # path-based allowlist must not be credited with inode isolation.
        os.link(fixture / "outside/secret", fixture / "work/hardlink-secret")
        os.link(fixture / "outside/sentinel", fixture / "work/hardlink-sentinel")
        if self.deny_hardlinks:
            # P0 candidate only. P1 must add admission/locking and concurrent
            # mutation handling; this scan alone is not a race-free boundary.
            aliases = [
                str(path)
                for path in (fixture / "work").rglob("*")
                if stat.S_ISREG((info := path.lstat()).st_mode) and info.st_nlink > 1
            ]
            config = json.loads(self.policy.read_text())
            config["filesystem"]["denyRead"].extend(aliases)
            config["filesystem"]["denyWrite"].extend(aliases)
            self.policy.write_text(json.dumps(config))
        code = r"""
const fs = require('node:fs');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const root = process.argv[1];
const results = {};
function attempt(name, fn) {
  try { results[name] = {ok: true, value: fn()}; }
  catch (e) { results[name] = {ok: false, code: e.code}; }
}
for (const file of ['work/normal','work/.env','work/secret','outside/secret',
                    'control/state.db','control/policy.json','work/outside-link/secret',
                    'work/hardlink-secret']) {
  attempt('read:' + file, () => fs.readFileSync(path.join(root,file),'utf8'));
}
for (const file of ['work/created','outside/sentinel','work/.env','work/outside-link/sentinel',
                    'work/hardlink-sentinel']) {
  attempt('write:' + file, () => {fs.writeFileSync(path.join(root,file),'changed');return true;});
}
attempt('link:outside', () => {
  fs.linkSync(path.join(root,'outside/secret'), path.join(root,'work/new-link'));return true;
});
attempt('link:denied-alias', () => {
  fs.linkSync(path.join(root,'work/hardlink-secret'), path.join(root,'work/new-alias'));return true;
});
attempt('rename:denied-alias', () => {
  fs.renameSync(path.join(root,'work/hardlink-secret'), path.join(root,'work/moved-alias'));
  return true;
});
attempt('rename:secret', () => {
  fs.renameSync(path.join(root,'work/secret'), path.join(root,'work/renamed'));return true;
});
const childCode = "try {require('node:fs').writeFileSync(process.argv[1],'grandchild');" +
                  "process.exit(99)} catch(e) {console.log(e.code)}";
const parentCode = "const c=require('node:child_process').spawnSync(process.execPath," +
  "['--eval',process.argv[1],'--',process.argv[2]],{encoding:'utf8'});" +
  "process.stdout.write(c.stdout);process.exit(c.status ?? 98)";
const child = spawnSync(process.execPath,['--eval',parentCode,'--',childCode,
  path.join(root,'outside/grandchild')],{encoding:'utf8'});
results.grandchild = {status:child.status,stdout:child.stdout,stderr:child.stderr};
console.log(JSON.stringify(results));
"""
        result = self.run("filesystem_and_descendants", code, str(fixture))
        try:
            values = json.loads(result["stdout"])
        except (ValueError, TypeError):
            return
        expected = {"read:work/normal", "write:work/created"}
        failed = [
            name
            for name, value in values.items()
            if name != "grandchild" and bool(value["ok"]) != (name in expected)
        ]
        if values["grandchild"]["status"] != 0 or (fixture / "outside/grandchild").exists():
            failed.append("grandchild")
        if (fixture / "outside/sentinel").read_text() != "unchanged":
            failed.append("outside sentinel changed")
        result.update(passed=result["returncode"] == 0 and not failed, failed_checks=failed)

    def argv_and_stdio(self) -> None:
        args = [
            "",
            "a b",
            "'quoted'",
            '"double"',
            "中文🙂",
            "line1\nline2",
            "\\",
            "--settings",
            "-c",
            "$(touch argv-pwned)",
            "`touch argv-pwned`",
            "; touch argv-pwned",
            "*",
        ]
        result = self.run("argv_exact", "console.log(JSON.stringify(process.argv.slice(1)))", *args)
        try:
            result["passed"] = result["returncode"] == 0 and json.loads(result["stdout"]) == args
        except ValueError:
            pass
        result["passed"] &= not (self.fixture / "work/argv-pwned").exists()
        result = self.run("stdin_utf8", "process.stdin.pipe(process.stdout)", input_text="输入🙂\n")
        result["passed"] = result["returncode"] == 0 and result["stdout"] == "输入🙂\n"
        code = "process.stdout.write('界'.repeat(100000));process.stderr.write('diagnostic')"
        result = self.run("large_output", code)
        result["passed"] = (
            result["returncode"] == 0
            and result["stdout"] == "界" * 100000
            and result["stderr"] == "diagnostic"
        )
        result["stdout_sha256"] = hashlib.sha256(result.pop("stdout").encode()).hexdigest()
        result["stdout_bytes"] = 300000 if result["passed"] else None

    def network(self) -> None:
        listeners: list[socket.socket] = []
        received: list[str] = []
        stop = threading.Event()
        endpoints = []

        def serve(server: socket.socket, label: str, udp: bool) -> None:
            while not stop.is_set():
                try:
                    if udp:
                        server.recvfrom(128)
                    else:
                        client, _ = server.accept()
                        client.close()
                    received.append(label)
                except TimeoutError:
                    continue
                except OSError:
                    return

        for name, family, kind, address in (
            ("tcp4", socket.AF_INET, socket.SOCK_STREAM, ("127.0.0.1", 0)),
            ("tcp6", socket.AF_INET6, socket.SOCK_STREAM, ("::1", 0)),
            ("udp4", socket.AF_INET, socket.SOCK_DGRAM, ("127.0.0.1", 0)),
            ("unix", socket.AF_UNIX, socket.SOCK_STREAM, str(self.fixture / "work/host.sock")),
        ):
            server = socket.socket(family, kind)
            try:
                server.bind(address)
                if kind == socket.SOCK_STREAM:
                    server.listen(10)
                server.settimeout(0.1)
            except OSError as exc:
                server.close()
                self.cases.append({"name": name + "_fixture", "passed": False, "error": str(exc)})
                continue
            listeners.append(server)
            location = server.getsockname()
            endpoints.append({"name": name, "location": location})
            threading.Thread(
                target=serve, args=(server, name, kind == socket.SOCK_DGRAM), daemon=True
            ).start()
        code = r"""
const net = require('node:net');const dgram = require('node:dgram');
const endpoints = JSON.parse(process.argv[1]);
Promise.all(endpoints.map(e => new Promise(resolve => {
  const s=e.name.startsWith('udp') ? dgram.createSocket('udp4') : new net.Socket();
  let finished=false;
  function done(outcome) {
    if(finished)return;finished=true;clearTimeout(timer);
    if(e.name.startsWith('udp'))s.close();else s.destroy();resolve({name:e.name,outcome});
  }
  const timer=setTimeout(()=>done('timeout'),1000);
  s.on('error',e=>done(e.code));
  if(e.name.startsWith('udp'))
    s.send('probe',e.location[1],e.location[0],e=>done(e?.code ?? 'sent'));
  else {s.on('connect',()=>done('connected'));
    if(e.name==='unix')s.connect(e.location);else s.connect(e.location[1],e.location[0]);}
}))).then(x=>console.log(JSON.stringify(x)));
"""
        try:
            # Establish positive controls before interpreting a denied connection.
            control = subprocess.run(
                [str(self.node), "--eval", code, "--", json.dumps(endpoints)],
                capture_output=True,
                text=True,
                timeout=10,
                env=self.env,
            )
            time.sleep(0.2)
            baseline = set(received)
            received.clear()
            result = self.run("direct_network_deny", code, json.dumps(endpoints))
            time.sleep(0.2)
            result.update(positive_control=sorted(baseline), received=received.copy())
            result["passed"] = (
                control.returncode == 0
                and result["returncode"] == 0
                and baseline == {e["name"] for e in endpoints}
                and len(endpoints) == 4
                and not received
            )
        finally:
            stop.set()
            for server in listeners:
                server.close()

    def invalid_config(self) -> None:
        original = self.policy.read_text()
        for name, content in (("empty_config", ""), ("invalid_config", "{broken")):
            self.policy.write_text(content)
            marker = self.fixture / "work" / name
            result = self.run(
                name, "require('node:fs').writeFileSync(process.argv[1],'executed')", str(marker)
            )
            result["passed"] = result["returncode"] != 0 and not marker.exists()
        self.policy.write_text(original)

    def allowlist(self) -> None:
        """Controlled HTTP positive case, redirect denial, and loopback SSRF.

        An explicit loopback IP:port is admitted only for the positive fixture.
        The separate SSRF listener is NOT admitted by IP. This does not verify
        public DNS rebinding, private-range routing, TLS, or SOCKS coverage.
        """
        requests_seen: list[str] = []
        ssrf_name = "127.0.0.1.nip.io"
        dns_code = (
            "require('node:dns').lookup(process.argv[1],{all:true},(e,a)=>"
            "{if(e){console.error(e.code);process.exit(1)}console.log(JSON.stringify(a))})"
        )
        control = subprocess.run(
            [str(self.node), "--eval", dns_code, "--", ssrf_name],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=10,
        )
        try:
            addresses = {item["address"] for item in json.loads(control.stdout)}
        except ValueError:
            addresses = set()
        if control.returncode != 0 or addresses != {"127.0.0.1"}:
            self.cases.append(
                {
                    "name": "ssrf_dns_fixture",
                    "passed": False,
                    "addresses": sorted(addresses),
                    "stderr": control.stderr,
                }
            )
            return

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests_seen.append(f"{self.server.server_port}:{self.path}")
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", f"http://{ssrf_name}:{ssrf.server_port}/secret")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"controlled-fixture")

            def log_message(self, *args):
                pass

        allowed = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        ssrf = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        for server in (allowed, ssrf):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        original = self.policy.read_text()
        config = json.loads(original)
        config["network"]["allowedDomains"] = [
            f"127.0.0.1:{allowed.server_port}",
            f"{ssrf_name}:{ssrf.server_port}",
        ]
        self.policy.write_text(json.dumps(config))
        code = r"""
const http=require('node:http');
const proxy=new URL(process.env.HTTP_PROXY);
// No child-side DNS exception: the parent-side proxy owns destination DNS.
const endpoint=proxy.hostname==='localhost' ? '127.0.0.1' : proxy.hostname;
const urls=JSON.parse(process.argv[1]);
function get(url,depth=0) {
  return new Promise(resolve=>{
    const target=new URL(url);
    const headers={Host:target.host};
    if(proxy.username || proxy.password)headers['Proxy-Authorization']='Basic '+Buffer.from(
      decodeURIComponent(proxy.username)+':'+decodeURIComponent(proxy.password)).toString('base64');
    const request=http.get({hostname:endpoint,port:proxy.port,path:url,headers},response=>{
      let body='';response.on('data',chunk=>body+=chunk);
      response.on('end',async()=>{
        if(response.statusCode===302 && depth<3)
          resolve(await get(response.headers.location,depth+1));
        else resolve({status:response.statusCode,body});
      });
    });
    request.on('error',e=>resolve({error:e.code}));
    request.setTimeout(2000,()=>request.destroy(new Error('timeout')));
  });
}
Promise.all(urls.map(url=>get(url))).then(results=>console.log(JSON.stringify(results)));
"""
        urls = [
            f"http://127.0.0.1:{allowed.server_port}/allowed",
            f"http://127.0.0.1:{ssrf.server_port}/not-allowlisted",
            f"http://{ssrf_name}:{ssrf.server_port}/secret",
            f"http://127.0.0.1:{allowed.server_port}/redirect",
        ]
        try:
            result = self.run("http_allowlist_redirect_loopback", code, json.dumps(urls))
            result["requests_seen"] = requests_seen.copy()
            result["dns_positive_control"] = {ssrf_name: sorted(addresses)}
            result["proxy_endpoint"] = "literal-loopback; no child DNS needed"
            try:
                values = json.loads(result["stdout"])
            except ValueError:
                return
            result["passed"] = (
                result["returncode"] == 0
                and [value.get("status") for value in values] == [200, 403, 403, 403]
                and values[0].get("body") == "controlled-fixture"
                and sorted(requests_seen)
                == sorted([f"{allowed.server_port}:/allowed", f"{allowed.server_port}:/redirect"])
            )
        finally:
            self.policy.write_text(original)
            for server in (allowed, ssrf):
                server.shutdown()
                server.server_close()

    def cancellation(self) -> None:
        heartbeat = self.fixture / "work/heartbeat"
        child_code = "setInterval(()=>require('node:fs').appendFileSync(process.argv[1],'.'),50)"
        code = (
            "const c=require('node:child_process').spawn(process.execPath,"
            + json.dumps(["--eval", child_code, "--", str(heartbeat)])
            + ",{stdio:'ignore',detached:true});console.log('ready');setInterval(()=>{},1000)"
        )
        process = subprocess.Popen(
            self.argv(code),
            cwd=self.fixture / "work",
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            close_fds=True,
        )
        identities = {}
        started = time.monotonic()
        while time.monotonic() - started < 10 and process.poll() is None:
            try:
                for child in psutil.Process(process.pid).children(recursive=True):
                    identities[child.pid] = (child, child.create_time(), child.name())
            except psutil.Error:
                pass
            if heartbeat.exists():
                break
            time.sleep(0.02)
        tree = [
            {"pid": pid, "started": born, "name": name}
            for pid, (_, born, name) in identities.items()
        ]
        if process.poll() is None:
            process.terminate()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            time.sleep(0.05)
        remaining = []
        for pid, (child, born, _) in identities.items():
            try:
                if child.create_time() == born and child.status() != psutil.STATUS_ZOMBIE:
                    remaining.append(pid)
                    child.kill()
            except psutil.Error:
                pass
        if process.poll() is None:
            process.kill()
        stdout, stderr = process.communicate(timeout=5)
        psutil.wait_procs([item[0] for item in identities.values()], timeout=3)
        self.cases.append(
            {
                "name": "cli_sigterm_descendant_cleanup",
                "passed": bool(tree) and heartbeat.exists() and not remaining,
                "tree": tree,
                "remaining_after_sigterm": remaining,
                "stdout": stdout,
                "stderr": stderr,
                "returncode": process.returncode,
                "probe_forced_cleanup": bool(remaining),
            }
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--helper", action="store_true", help="Use the pinned bot SRT library adapter"
    )
    parser.add_argument(
        "--deny-hardlinks",
        action="store_true",
        help="P0 candidate: compile existing hardlink aliases as OS denies",
    )
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    lock = json.loads((runtime / "runtime-lock.json").read_text())
    package = json.loads(
        (runtime / "node_modules/@anthropic-ai/sandbox-runtime/package.json").read_text()
    )
    node_version = subprocess.check_output(
        [str(runtime / "node/bin/node"), "--version"],
        text=True,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
    ).strip()
    if node_version != "v" + lock["node_version"] or package["version"] != lock["srt_version"]:
        raise SystemExit("runtime version mismatch")
    with tempfile.TemporaryDirectory(prefix="bot-native-p0-", dir="/tmp") as temp:
        fixture = Path(temp).resolve()
        for name in ("work", "outside", "control", "home", "tmp", "cache"):
            (fixture / name).mkdir()
        probe = Probe(runtime, fixture, deny_hardlinks=args.deny_hardlinks, helper=args.helper)
        config = settings(runtime, fixture)
        probe.policy.write_text(json.dumps(config))
        probe.filesystem()
        probe.argv_and_stdio()
        probe.network()
        probe.allowlist()
        probe.invalid_config()
        probe.cancellation()
        evidence = {
            "phase": "P0",
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "node": node_version,
            "srt": package["version"],
            "policy": json.loads(probe.policy.read_text()),
            "deny_hardlinks": args.deny_hardlinks,
            "launcher": "bot-library-helper" if args.helper else "srt-cli",
            "helper_sha256": hashlib.sha256((runtime / "launcher.mjs").read_bytes()).hexdigest()
            if args.helper
            else None,
            "probe_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "cases": probe.cases,
            "passed": all(case["passed"] for case in probe.cases),
            "allowlist": "unverified",
            "release_ready": False,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
        for case in probe.cases:
            print(f"{'PASS' if case['passed'] else 'FAIL'} {case['name']}", flush=True)
        raise SystemExit(0 if evidence["passed"] else 1)


if __name__ == "__main__":
    main()

"""Real OS checks. Explicit release runs fail rather than skip missing runtimes.

BOT_TEST_SANDBOX_RUNTIME selects a host-installed pinned runtime for these tests.
BOT_REQUIRE_NATIVE_SANDBOX=1 makes absence a test failure. Run outside an enclosing
Seatbelt sandbox. Current tests prove the compiler boundary, not full tool wiring.
"""

import fcntl
import json
import os
import signal
import socket
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

from bot.config.models import AppConfig
from bot.sandbox.policy import RunDirectories, resolve_policy
from bot.sandbox.runtime import inspect_runtime
from bot.sandbox.srt import compile_srt_config, launcher_environment, system_read_paths


@pytest.fixture
def native(tmp_path, monkeypatch, request):
    name = os.environ.get("BOT_TEST_SANDBOX_RUNTIME")
    if not name:
        if os.environ.get("BOT_REQUIRE_NATIVE_SANDBOX") == "1":
            pytest.fail("release validation requires BOT_TEST_SANDBOX_RUNTIME")
        pytest.skip("native runtime not selected")
    runtime = Path(name).resolve()
    work, host_home, outside = tmp_path / "work", tmp_path / "host-home", tmp_path / "outside"
    directories = RunDirectories(tmp_path / "runs/first")
    temporary = tempfile.TemporaryDirectory(prefix="bot-srt-", dir="/tmp")
    request.addfinalizer(temporary.cleanup)
    control = Path(temporary.name).resolve()
    sibling = tmp_path / "runs/sibling"
    for path in (
        work,
        host_home / ".ssh",
        outside,
        directories.home,
        directories.temp,
        directories.cache,
        control,
        sibling,
        work / ".bot",
    ):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(host_home))
    inspection = inspect_runtime(work, root=runtime)
    if not inspection.metadata_valid:
        pytest.fail(str(inspection.errors))
    sentinel = outside / "sentinel"
    sentinel.write_text("outside-synthetic-secret")
    (host_home / ".ssh/id_rsa").write_text("synthetic-ssh-key")
    (work / ".env").write_text("FAKE_KEY=synthetic-secret")
    (work / ".bot/state.db").write_text("synthetic-state")
    (work / ".bot/config.toml").write_text("synthetic-config")
    (work / "input.txt").write_text("正常内容")
    (sibling / "secret").write_text("synthetic-sibling-secret")
    config = AppConfig.model_validate(
        {"sandbox": {"mode": "required"}, "storage": {"state_path": ".bot/state.db"}}
    )
    node = runtime / "node/bin/node"
    launcher = runtime / "launcher.mjs"

    def run(code, *args, readonly=False, extra_read=()):
        selected = config.model_copy(
            update={"sandbox": config.sandbox.model_copy(update={"read_roots": extra_read})}
        )
        resolved = resolve_policy(
            selected,
            workspace=work,
            directories=directories,
            runtime_read_roots=(runtime, *system_read_paths()),
            read_only=readonly,
        )
        settings = control / "policy.json"
        settings.write_text(json.dumps(compile_srt_config(resolved.policy)))
        env = launcher_environment(resolved.policy, runtime_root=runtime, control_temp=control)
        resolved.admission.verify()
        argv = [
            str(node),
            str(launcher),
            str(settings),
            "--",
            str(node),
            "--eval",
            code,
            "--",
            *map(str, args),
        ]
        process = subprocess.Popen(
            argv,
            cwd=work,
            env=env,
            close_fds=True,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
            pytest.fail("SRT target timed out; bounded fixture process group was killed")
        assert process.returncode == 0, (process.returncode, stdout, stderr)
        return json.loads(stdout)

    return run, work, sentinel, host_home, directories, control, sibling


def test_native_compiled_policy_preserves_argv_and_workspace_io(native):
    run, work, *_ = native
    args = ("", "a b", "a'b", 'a"b', "a\nb", "中文", "$(not-a-shell)", "; harmless")
    result = run(
        """const fs=require('fs');
fs.writeFileSync('result.txt',fs.readFileSync('input.txt','utf8'));
console.log(JSON.stringify(process.argv.slice(1)));""",
        *args,
    )
    assert result == list(args)
    assert (work / "result.txt").read_text() == "正常内容"


def test_native_compiled_policy_denies_secrets_control_and_other_runs(native):
    run, work, sentinel, home, _, control, sibling = native
    paths = [
        sentinel,
        home / ".ssh/id_rsa",
        work / ".env",
        work / ".bot/state.db",
        work / ".bot/config.toml",
        control / "policy.json",
        sibling / "secret",
    ]
    result = run(
        """const fs=require('fs');
console.log(JSON.stringify(process.argv.slice(1).map(p=>{
try{return {data:fs.readFileSync(p,'utf8')}}catch(e){return {error:e.code}}})));""",
        *paths,
    )
    assert all(
        "data" not in item and item["error"] in {"EPERM", "EACCES", "ENOENT"} for item in result
    )


def test_native_compiled_policy_denies_external_write_and_child_write(native):
    run, _, sentinel, *_ = native
    result = run(
        """const fs=require('fs'), cp=require('child_process'); let own;
try{fs.writeFileSync(process.argv[1],'bad');own='allowed'}catch(e){own=e.code}
const child=cp.spawnSync(process.execPath,['-e',
'try{require("fs").writeFileSync(process.argv[1],"bad");process.exit(11)}catch(e){process.exit(0)}',
process.argv[1]],{stdio:'pipe'}); console.log(JSON.stringify({own,child:child.status}));""",
        sentinel,
    )
    assert result["own"] in {"EPERM", "EACCES", "EROFS", "ENOENT"}
    assert result["child"] == 0
    assert sentinel.read_text() == "outside-synthetic-secret"


def test_native_compiled_policy_denies_existing_and_new_cross_boundary_hardlinks(native):
    run, work, sentinel, *_ = native
    alias = work / "alias"
    os.link(sentinel, alias)
    result = run(
        """const fs=require('fs'); let results=[];
for(const action of [()=>fs.readFileSync('alias'),()=>fs.writeFileSync('alias','bad'),
()=>fs.renameSync('alias','moved'),()=>fs.linkSync(process.argv[1],'new-alias')]){
try{action();results.push('allowed')}catch(e){results.push(e.code)}}
console.log(JSON.stringify(results));""",
        sentinel,
        extra_read=(sentinel.parent,),
    )
    assert all(value in {"EPERM", "EACCES", "EROFS", "ENOENT"} for value in result[:2]), result
    assert result[2] in {"EPERM", "EACCES", "EROFS", "ENOENT", "EBUSY"}, result
    assert result[3] in {"EPERM", "EACCES", "EROFS", "ENOENT", "EXDEV"}, result
    assert sentinel.read_text() == "outside-synthetic-secret"
    assert not (work / "new-alias").exists()


def test_native_cannot_link_a_readable_readonly_file_into_workspace(native):
    run, work, sentinel, *_ = native
    assert sentinel.stat().st_nlink == 1
    result = run(
        """const fs=require('fs'); const data=fs.readFileSync(process.argv[1],'utf8'); let link;
try{fs.linkSync(process.argv[1],'new-alias');link='allowed'}catch(e){link=e.code}
console.log(JSON.stringify({data,link}));""",
        sentinel,
        extra_read=(sentinel.parent,),
    )
    assert result["data"] == "outside-synthetic-secret"
    assert result["link"] in {"EPERM", "EACCES", "EROFS", "EXDEV"}
    assert not (work / "new-alias").exists()


def test_native_protected_configuration_alias_cannot_be_unlinked_or_renamed(native):
    run, work, sentinel, *_ = native
    alias = work / ".bot/config.toml"
    alias.unlink()
    alias.symlink_to(sentinel)
    result = run(
        """const fs=require('fs');let results=[];
fs.writeFileSync('ordinary-source.txt','project writes still work');
for(const action of [()=>fs.readFileSync('.bot/config.toml'),
()=>fs.unlinkSync('.bot/config.toml'),()=>fs.renameSync('.bot','moved-bot')]){
try{action();results.push('allowed')}catch(e){results.push(e.code)}}
console.log(JSON.stringify(results));"""
    )
    assert all(value in {"EPERM", "EACCES", "EROFS", "EBUSY", "ENOENT"} for value in result)
    assert alias.is_symlink()
    assert sentinel.read_text() == "outside-synthetic-secret"
    assert (work / "ordinary-source.txt").read_text() == "project writes still work"


def test_native_readonly_allows_private_tmp_but_denies_workspace_writes(native):
    run, work, _, _, directories, *_ = native
    result = run(
        """const fs=require('fs'),path=require('path');let denied;
try{fs.writeFileSync('input.txt','bad');denied=false}catch(e){denied=true}
fs.writeFileSync(path.join(process.env.TMPDIR,'ok'),'private');
console.log(JSON.stringify({denied,tmp:process.env.TMPDIR}));""",
        readonly=True,
    )
    assert result["denied"]
    assert (work / "input.txt").read_text() == "正常内容"
    assert Path(result["tmp"]) == directories.temp
    assert (directories.temp / "ok").read_text() == "private"


def test_native_srt_implicit_shared_tmp_write_is_overridden(native):
    run, *_ = native
    shared = Path("/tmp/claude")
    shared.mkdir(exist_ok=True)
    sentinel = shared / f"bot-test-{uuid.uuid4().hex}"
    sentinel.write_text("synthetic-shared-sentinel")  # Positive host access control.
    try:
        result = run(
            """const fs=require('fs');let read,write;
try{read=fs.readFileSync(process.argv[1],'utf8')}catch(e){read=e.code}
try{fs.writeFileSync(process.argv[1],'bad');write='allowed'}catch(e){write=e.code}
console.log(JSON.stringify({read,write}));""",
            sentinel,
        )
        assert result["read"] in {"EPERM", "EACCES", "ENOENT"}
        assert result["write"] in {"EPERM", "EACCES", "EROFS", "ENOENT"}
        assert sentinel.read_text() == "synthetic-shared-sentinel"
    finally:
        sentinel.unlink(missing_ok=True)


def test_native_compiled_network_deny_blocks_live_loopback_endpoint(native):
    run, *_ = native
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(3)
        host, port = listener.getsockname()
        with socket.create_connection((host, port), timeout=2):
            pass
        result = run(
            """const s=require('net').connect(Number(process.argv[1]),'127.0.0.1');
s.once('connect',()=>{console.log(JSON.stringify('connected'));s.destroy()});
s.once('error',e=>console.log(JSON.stringify(e.code)));s.setTimeout(2000,()=>s.destroy());""",
            port,
        )
        assert result in {"EPERM", "EACCES", "ENETUNREACH", "ECONNREFUSED"}


def test_native_proxy_works_without_exposing_control_files_or_other_sockets(native):
    run, _, _, _, _, control, _ = native
    secret = control / "fake-secret"
    secret.write_text("synthetic-control-secret")
    path = control / "unrelated.sock"
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(path))
        listener.listen(3)
        with socket.socket(socket.AF_UNIX) as positive:
            positive.connect(str(path))
            connection, _ = listener.accept()
            connection.close()
        result = run(
            r"""const fs=require('fs'),net=require('net'),http=require('http');
const reads=process.argv.slice(1,3).map(p=>{
  try{return {data:fs.readFileSync(p,'utf8')}}catch(e){return {error:e.code}}
});
const unix=new Promise(resolve=>{
  const s=net.createConnection({path:process.argv[3]});
  s.on('connect',()=>{s.destroy();resolve('connected')});
  s.on('error',e=>resolve(e.code));s.setTimeout(1000,()=>{s.destroy();resolve('timeout')});
});
const proxy=new Promise(resolve=>{
  const p=new URL(process.env.HTTP_PROXY),headers={Host:'127.0.0.1:9'};
  if(p.username)headers['Proxy-Authorization']='Basic '+Buffer.from(
    decodeURIComponent(p.username)+':'+decodeURIComponent(p.password)).toString('base64');
  const r=http.get({host:p.hostname,port:p.port,path:'http://127.0.0.1:9/',headers},res=>{
    res.resume();resolve(res.statusCode);
  });
  r.on('error',e=>resolve(e.code));r.setTimeout(2000,()=>{r.destroy();resolve('timeout')});
});
Promise.all([unix,proxy]).then(([unix,proxy])=>console.log(JSON.stringify({reads,unix,proxy})));""",
            secret,
            control / "policy.json",
            path,
        )
        assert result["proxy"] == 403, result
        assert result["unix"] in {"EPERM", "EACCES", "ENOENT"}, result
        assert all(item.get("error") in {"EPERM", "EACCES", "ENOENT"} for item in result["reads"])
        listener.settimeout(0.05)
        with pytest.raises(TimeoutError):
            listener.accept()
    assert secret.read_text() == "synthetic-control-secret"


def test_native_environment_excludes_host_keys_and_loaders(native, monkeypatch):
    run, _, _, _, directories, *_ = native
    monkeypatch.setenv("NODE_OPTIONS", "--require=/untrusted-hook")
    monkeypatch.setenv("FAKE_MODEL_API_KEY", "synthetic-key")
    result = run("""console.log(JSON.stringify({home:process.env.HOME,tmp:process.env.TMPDIR,
key:process.env.FAKE_MODEL_API_KEY??null,loader:process.env.NODE_OPTIONS??null}));""")
    assert result == {
        "home": str(directories.home),
        "tmp": str(directories.temp),
        "key": None,
        "loader": None,
    }


def test_native_launch_closes_inheritable_host_descriptors(native):
    run, *_ = native
    with tempfile.TemporaryFile() as stream:
        stream.write(b"synthetic-descriptor-secret")
        stream.seek(0)
        descriptor = fcntl.fcntl(stream.fileno(), fcntl.F_DUPFD, 128)
        os.set_inheritable(descriptor, True)
        try:
            assert os.read(descriptor, 64) == b"synthetic-descriptor-secret"
            os.lseek(descriptor, 0, os.SEEK_SET)
            result = run(
                """try{console.log(JSON.stringify({data:
require('fs').readFileSync(Number(process.argv[1]),'utf8')}))}
catch(e){console.log(JSON.stringify({error:e.code}))}""",
                descriptor,
            )
            assert result == {"error": "EBADF"}
        finally:
            os.close(descriptor)

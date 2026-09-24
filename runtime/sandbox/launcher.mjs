// Fixed SRT 0.0.77 adapter. Runs on the trusted side, once per invocation.
// Low-level wrappers are intentional: SandboxManager.wrapWithSandbox always
// adds shared /tmp/claude write roots, which also re-expose reads on Linux.
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { SandboxManager, SandboxRuntimeConfigSchema } from './node_modules/@anthropic-ai/sandbox-runtime/dist/index.js';
import { wrapCommandWithSandboxLinux } from './node_modules/@anthropic-ai/sandbox-runtime/dist/sandbox/linux-sandbox-utils.js';
import { wrapCommandWithSandboxMacOS } from './node_modules/@anthropic-ai/sandbox-runtime/dist/sandbox/macos-sandbox-utils.js';
import { quote } from './node_modules/@anthropic-ai/sandbox-runtime/dist/utils/shell-quote.js';

// This shim executes INSIDE the OS sandbox. No target argument becomes host JS.
// execve preserves argv/stdin/streams without leaving another process parent.
const INNER = `
import fs from 'node:fs';
import path from 'node:path';
const argv = process.argv.slice(1);
const env = {...process.env};
for (const name of ['HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','FTP_PROXY',
                    'http_proxy','https_proxy','all_proxy','ftp_proxy',
                    'DOCKER_HTTP_PROXY','DOCKER_HTTPS_PROXY']) {
  if (!env[name]) continue;
  const url = new URL(env[name]);
  if (url.hostname === 'localhost') url.hostname = '127.0.0.1';
  env[name] = url.toString();
}
env.NO_PROXY = env.no_proxy = '';
if (env.CLOUDSDK_PROXY_ADDRESS === 'localhost') env.CLOUDSDK_PROXY_ADDRESS = '127.0.0.1';
if (env.RSYNC_PROXY?.startsWith('localhost:')) env.RSYNC_PROXY = env.RSYNC_PROXY.replace('localhost:', '127.0.0.1:');
if (env.GIT_SSH_COMMAND) env.GIT_SSH_COMMAND = env.GIT_SSH_COMMAND.replace(/\\blocalhost\\b/g, '127.0.0.1');
let executable;
if (argv[0].includes('/')) executable = path.resolve(argv[0]);
else for (const directory of env.PATH.split(':')) {
  const candidate = path.join(directory, argv[0]);
  try { fs.accessSync(candidate, fs.constants.X_OK); if (fs.statSync(candidate).isFile()) { executable = candidate; break; } } catch {}
}
if (!executable) throw new Error('command executable not found in isolated PATH');
process.execve(executable, argv, env);
`;

let child;
let timer;
let requestedSignal;
let statusFd;
let statusId;
let statusValid = false;
let statusSent = false;
let phase = 'initialize';
let possibleExecution = false;

function status(state) {
  const data = Buffer.from(JSON.stringify({v:1,id:statusId,state,stage:phase})+'\n');
  if (fs.writeSync(statusFd, data) !== data.length) throw new Error('incomplete status write');
  if (state !== 'initializing') {
    statusSent = true;
    fs.closeSync(statusFd);
    statusFd = undefined;
  }
}
for (const name of ['SIGTERM', 'SIGINT']) {
  process.on(name, () => {
    requestedSignal = name;
    child?.kill(name);
    timer ??= setTimeout(() => child?.kill('SIGKILL'), 2000).unref();
  });
}

try {
  const [fdFlag, fdText, idFlag, invocation, settingsPath, separator, ...argv] = process.argv.slice(2);
  if (fdFlag !== '--status-fd' || !/^[0-9]+$/.test(fdText ?? '') || Number(fdText) < 3 ||
      idFlag !== '--status-id' || !/^[0-9a-f]{32}$/.test(invocation ?? '') ||
      !settingsPath || separator !== '--' || argv.length === 0) {
    throw new Error('expected status fd/id, explicit policy path, --, and target argv');
  }
  statusFd = Number(fdText);
  statusId = invocation;
  if (!Number.isSafeInteger(statusFd) || !fs.fstatSync(statusFd).isFIFO()) {
    throw new Error('status descriptor must be a pipe');
  }
  // pass_fds clears CLOEXEC. On Linux, Node/libuv does not consistently close
  // arbitrary inherited high-numbered descriptors when spawning children.
  // Reopen through Node's fs (libuv opens with CLOEXEC), then retire the
  // inherited copy BEFORE SRT can spawn proxies or sandbox helpers.
  const inheritedStatusFd = statusFd;
  statusFd = fs.openSync(`/dev/fd/${inheritedStatusFd}`, fs.constants.O_WRONLY);
  fs.closeSync(inheritedStatusFd);
  statusValid = true;
  // A broken or read-only descriptor fails before any sandbox/target dispatch.
  status('initializing');
  if (process.versions.node !== '24.14.0') throw new Error('pinned Node 24.14.0 required');
  const packageFile = fileURLToPath(new URL('./node_modules/@anthropic-ai/sandbox-runtime/package.json', import.meta.url));
  if (JSON.parse(fs.readFileSync(packageFile, 'utf8')).version !== '0.0.77') {
    throw new Error('pinned SRT 0.0.77 required');
  }
  phase = 'policy';
  const fd = fs.openSync(settingsPath, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
  let raw;
  try {
    const size = fs.fstatSync(fd).size;
    if (size < 1 || size > 2_000_000) throw new Error('invalid policy size');
    raw = fs.readFileSync(fd, 'utf8');
  } finally { fs.closeSync(fd); }
  const config = SandboxRuntimeConfigSchema.parse(JSON.parse(raw));
  if (config.filesystem.disabled || !config.filesystem.denyRead.includes('/') ||
      !config.filesystem.allowRead?.length || !config.filesystem.allowWrite.length ||
      config.enableWeakerNestedSandbox || config.enableWeakerNetworkIsolation ||
      config.network.allowAllUnixSockets || config.network.allowLocalBinding) {
    throw new Error('required filesystem and network restrictions missing');
  }
  phase = 'initialize';
  await SandboxManager.initialize(config, undefined, false);
  if (!(await SandboxManager.waitForNetworkInitialization())) throw new Error('proxy initialization failed');
  if (requestedSignal) throw new Error('launch cancelled before target start');
  const httpSocketPath = SandboxManager.getLinuxHttpSocketPath();
  const socksSocketPath = SandboxManager.getLinuxSocksSocketPath();
  const proxySockets = process.platform === 'linux' ? [...new Set([httpSocketPath, socksSocketPath])] : [];
  for (const socketPath of proxySockets) {
    if (typeof socketPath !== 'string' || path.dirname(socketPath) !== process.env.TMPDIR ||
        !fs.lstatSync(socketPath).isSocket()) {
      throw new Error('proxy socket is not a direct socket in the owned control directory');
    }
  }
  const command = quote([process.execPath, '--input-type=module', '--eval', INNER, '--', ...argv]);
  const params = {
    command,
    needsNetworkRestriction: true,
    httpProxyPort: SandboxManager.getProxyPort(),
    socksProxyPort: SandboxManager.getSocksProxyPort(),
    proxyAuthToken: SandboxManager.getProxyAuthToken(),
    httpSocketPath,
    socksSocketPath,
    // SRT mounts proxy sockets BEFORE filesystem masks. Rebind only these
    // endpoints after denyRead('/') masks /tmp; never expose their parent or
    // policy files. The target's AF_UNIX seccomp restriction stays in place.
    readConfig: {
      denyOnly: config.filesystem.denyRead,
      allowWithinDeny: [...config.filesystem.allowRead, ...proxySockets],
    },
    writeConfig: {
      allowOnly: [...config.filesystem.allowWrite, '/dev/null', '/dev/stdout', '/dev/stderr'],
      denyWithinAllow: config.filesystem.denyWrite,
    },
    allowUnixSockets: [], allowAllUnixSockets: false, allowLocalBinding: false,
    allowPty: false, allowGitConfig: false, allowAppleEvents: false,
    enableWeakerNestedSandbox: false, enableWeakerNetworkIsolation: false,
    binShell: '/bin/bash',
    bwrapPath: fileURLToPath(new URL('./bwrap-launcher.sh', import.meta.url)),
  };
  phase = 'wrap';
  let wrapped;
  if (process.platform === 'linux') wrapped = await wrapCommandWithSandboxLinux(params);
  else if (process.platform === 'darwin') wrapped = await wrapCommandWithSandboxMacOS(params);
  else throw new Error('only macOS/Linux supported');
  if (requestedSignal) throw new Error('launch cancelled before target start');
  phase = 'spawn';
  possibleExecution = true;
  // Standard streams are inherited; the status pipe's CLOEXEC prevents it
  // from being inherited by the sandbox or target.
  child = spawn('/bin/sh', ['-c', wrapped], { stdio: 'inherit', env: process.env });
  const completion = new Promise(resolve => {
    child.once('error', error => resolve({ error }));
    child.once('close', (code, signal) => resolve({ code, signal }));
  });
  const spawned = await new Promise(resolve => {
    child.once('spawn', () => resolve(true));
    child.once('error', () => resolve(false));
  });
  if (!spawned) {
    possibleExecution = false;
    throw new Error('sandbox wrapper spawn failed');
  }
  status('dispatched');
  const result = await completion;
  if (result.error) throw result.error;
  process.exitCode = result.code ?? (result.signal === 'SIGINT' ? 130 : 143);
} catch (error) {
  if (!statusSent && statusValid && statusFd !== undefined) {
    try { status(possibleExecution ? 'unknown' : 'failed'); } catch {}
  }
  if (possibleExecution && !statusSent) child?.kill('SIGTERM');
  console.error('Sandbox launcher failed: ' + String(error.message).slice(0, 2000));
  process.exitCode = requestedSignal === 'SIGINT' ? 130 : 1;
} finally {
  clearTimeout(timer);
  if (statusFd !== undefined) { try { fs.closeSync(statusFd); } catch {} }
  SandboxManager.cleanupAfterCommand();
  await SandboxManager.reset();
}

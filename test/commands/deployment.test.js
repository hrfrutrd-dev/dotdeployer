import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { chmod, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { deployProject, verifyProject } from '../../src/commands/deployment.js';

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const identitySentinel = 'SENSITIVE_PROVIDER_IDENTITY_MUST_NOT_APPEAR';

async function makeProject(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'dotdeployer-deploy-test-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  return root;
}

async function write(root, relative, content) {
  const target = path.join(root, relative);
  await mkdir(path.dirname(target), { recursive: true });
  await writeFile(target, content);
}

async function fakeCli(binDirectory, name, source) {
  const executable = path.join(binDirectory, name);
  await writeFile(executable, source);
  await chmod(executable, 0o755);
}

async function makeProviderProject(root, provider) {
  if (provider === 'render') {
    await write(root, 'render.yaml', [
      'services:',
      '  - type: web',
      '    name: api',
      '    startCommand: node app.js',
    ].join('\n'));
    await write(root, 'app.js', 'console.log("render");');
  } else if (provider === 'railway') {
    await write(root, 'railway.toml', '[deploy]\nstartCommand = "node app.js"\n');
    await write(root, 'app.js', 'console.log("railway");');
  } else if (provider === 'vercel') {
    await write(root, 'vercel.json', '{}');
  } else if (provider === 'cloudflare') {
    await write(root, 'wrangler.toml', [
      'name = "verify-worker"',
      'main = "src/index.js"',
      'compatibility_date = "2026-10-01"',
      '',
      '[env.staging]',
      'name = "verify-worker-staging"',
    ].join('\n'));
    await write(root, 'src/index.js', 'export default {};');
  } else if (provider === 'gas') {
    await write(root, 'appsscript.json', JSON.stringify({ timeZone: 'Etc/UTC', runtimeVersion: 'V8' }));
    await write(root, 'Code.gs', 'function run() { return true; }');
  }
}

const identityProbeCli = `#!/bin/sh
if [ "$1" = "--version" ]; then echo 'mock-provider-cli 1.2.3'; exit 0; fi
if [ "$1" = "whoami" ] || { [ "$1" = "login" ] && [ "$2" = "--status" ]; }; then
  echo '${identitySentinel}'
  exit 0
fi
exit 9
`;

const wranglerCli = `#!/bin/sh
printf '%s\\n' "$*" >> "$DOTDEPLOYER_WRANGLER_TRACE"
if [ "$1" = "--version" ]; then echo 'wrangler 4.99.0'; exit 0; fi
if [ "$1" = "whoami" ]; then echo '${identitySentinel}'; exit 0; fi
if [ "$1" = "deploy" ] && [ "$2" = "--dry-run" ]; then
  while [ "$#" -gt 0 ]; do
    if [ "$1" = "--outdir" ]; then
      shift
      mkdir -p "$1"
      if [ "$DOTDEPLOYER_LARGE_OUTPUT" = "1" ]; then
        node -e 'require("node:fs").writeFileSync(process.argv[1] + "/large.bin", Buffer.alloc(20000000))' "$1"
      fi
      break
    fi
    shift
  done
  echo '${identitySentinel}' >&2
  exit 0
fi
if [ "$1" = "deploy" ] && [ "$2" = "--env" ]; then echo 'remote deploy accepted'; exit 0; fi
exit 8
`;

test('verifies live CLI access for every supported provider without retaining identity output', async (t) => {
  const bin = await mkdtemp(path.join(os.tmpdir(), 'dotdeployer-verify-bin-'));
  t.after(() => rm(bin, { recursive: true, force: true }));
  const providerClis = [
    ['render', 'render'],
    ['railway', 'railway'],
    ['vercel', 'vercel'],
    ['cloudflare', 'wrangler'],
    ['gas', 'clasp'],
  ];
  for (const [, cli] of providerClis) await fakeCli(bin, cli, identityProbeCli);

  for (const [provider] of providerClis) {
    const root = await makeProject(t);
    await makeProviderProject(root, provider);
    const result = await verifyProject({
      projectRoot: root,
      provider,
      timeoutSeconds: 5,
      env: { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH}` },
    });
    assert.equal(result.ok, true, `${provider}: ${JSON.stringify(result.issues)}`);
    assert.equal(result.authentication.authenticated, true);
    assert.equal(result.commands.length, 2);
    assert.equal(JSON.stringify(result).includes(identitySentinel), false);
  }
});

test('simulates a named Worker deployment after environment-specific dry-run and never publishes', async (t) => {
  const root = await makeProject(t);
  const bin = path.join(root, 'fake-bin');
  await mkdir(bin);
  const trace = path.join(root, 'commands.log');
  await makeProviderProject(root, 'cloudflare');
  await fakeCli(bin, 'wrangler', wranglerCli);
  const env = {
    ...process.env,
    PATH: `${bin}${path.delimiter}${process.env.PATH}`,
    DOTDEPLOYER_WRANGLER_TRACE: trace,
  };

  const result = await deployProject({
    projectRoot: root,
    provider: 'cloudflare',
    environment: 'staging',
    env,
    timeoutSeconds: 5,
  });
  assert.equal(result.ok, true, JSON.stringify(result.issues));
  assert.equal(result.mode, 'simulation');
  assert.equal(result.deployed, false);
  assert.equal(result.environmentDryRunPassed, true);
  assert.ok(result.commands.some((item) => item.name === 'wrangler deploy --env staging' && item.planned));
  const calls = await readFile(trace, 'utf8');
  assert.match(calls, /deploy --dry-run --env staging/);
  assert.doesNotMatch(calls, /^deploy --env staging$/m);
  assert.equal(JSON.stringify(result).includes(identitySentinel), false);
});

test('blocks simulation and publish when Wrangler dry-run output exceeds the artifact limit', async (t) => {
  const root = await makeProject(t);
  const bin = path.join(root, 'fake-bin');
  await mkdir(bin);
  const trace = path.join(root, 'commands.log');
  await makeProviderProject(root, 'cloudflare');
  await fakeCli(bin, 'wrangler', wranglerCli);
  const env = {
    ...process.env,
    PATH: `${bin}${path.delimiter}${process.env.PATH}`,
    DOTDEPLOYER_WRANGLER_TRACE: trace,
    DOTDEPLOYER_LARGE_OUTPUT: '1',
  };

  const result = await deployProject({
    projectRoot: root,
    provider: 'cloudflare',
    environment: 'staging',
    execute: true,
    confirmed: true,
    env,
    timeoutSeconds: 10,
  });
  assert.equal(result.ok, false);
  assert.equal(result.deployed, false);
  assert.equal(result.environmentDryRunPassed, false);
  assert.ok(result.issues.some((item) => item.code === 'FILE_TOO_LARGE'));
  const calls = await readFile(trace, 'utf8');
  assert.doesNotMatch(calls, /^deploy --env staging$/m);
});

test('executes only a confirmed non-production Worker deployment after matching dry-run', async (t) => {
  const root = await makeProject(t);
  const bin = path.join(root, 'fake-bin');
  await mkdir(bin);
  const trace = path.join(root, 'commands.log');
  await makeProviderProject(root, 'cloudflare');
  await fakeCli(bin, 'wrangler', wranglerCli);
  const env = {
    ...process.env,
    PATH: `${bin}${path.delimiter}${process.env.PATH}`,
    DOTDEPLOYER_WRANGLER_TRACE: trace,
  };

  const result = await deployProject({
    projectRoot: root,
    provider: 'cloudflare',
    environment: 'staging',
    execute: true,
    confirmed: true,
    env,
    timeoutSeconds: 5,
  });
  assert.equal(result.ok, true, JSON.stringify(result.issues));
  assert.equal(result.deployed, true);
  const calls = await readFile(trace, 'utf8');
  assert.match(calls, /^deploy --env staging$/m);
  assert.ok(result.commands.some((item) => item.name === 'wrangler deploy --env staging' && item.exitCode === 0));
  assert.equal(JSON.stringify(result).includes(identitySentinel), false);
});

test('refuses production and unconfirmed execution before invoking provider commands', async (t) => {
  const root = await makeProject(t);
  const bin = path.join(root, 'fake-bin');
  await mkdir(bin);
  const trace = path.join(root, 'commands.log');
  await makeProviderProject(root, 'cloudflare');
  await fakeCli(bin, 'wrangler', wranglerCli);
  const env = {
    ...process.env,
    PATH: `${bin}${path.delimiter}${process.env.PATH}`,
    DOTDEPLOYER_WRANGLER_TRACE: trace,
  };

  const unconfirmed = await deployProject({
    projectRoot: root,
    provider: 'cloudflare',
    environment: 'staging',
    execute: true,
    confirmed: false,
    env,
  });
  assert.equal(unconfirmed.ok, false);
  assert.equal(unconfirmed.issues[0].code, 'DEPLOY_CONFIRMATION_REQUIRED');

  const production = await deployProject({
    projectRoot: root,
    provider: 'cloudflare',
    environment: 'production',
    execute: true,
    confirmed: true,
    env,
  });
  assert.equal(production.ok, false);
  assert.equal(production.issues[0].code, 'PRODUCTION_DEPLOY_BLOCKED');
  await assert.rejects(readFile(trace, 'utf8'), { code: 'ENOENT' });
});

test('blocks a real deploy when an .env file would enter the staged context', async (t) => {
  const root = await makeProject(t);
  const bin = path.join(root, 'fake-bin');
  await mkdir(bin);
  const trace = path.join(root, 'commands.log');
  await makeProviderProject(root, 'cloudflare');
  await write(root, '.env.local', 'PRIVATE_VALUE=not-a-real-secret\n');
  await fakeCli(bin, 'wrangler', wranglerCli);
  const env = {
    ...process.env,
    PATH: `${bin}${path.delimiter}${process.env.PATH}`,
    DOTDEPLOYER_WRANGLER_TRACE: trace,
  };

  const result = await deployProject({
    projectRoot: root,
    provider: 'cloudflare',
    environment: 'staging',
    execute: true,
    confirmed: true,
    env,
    timeoutSeconds: 5,
  });
  assert.equal(result.ok, false);
  assert.ok(result.issues.some((item) => item.code === 'DEPLOY_ENV_FILE_BLOCKED'));
  const calls = await readFile(trace, 'utf8');
  assert.doesNotMatch(calls, /deploy --dry-run/);
  assert.doesNotMatch(calls, /^deploy --env staging$/m);
});

test('CLI verify and deploy JSON modes are usable; execute requires an explicit --yes', async (t) => {
  const root = await makeProject(t);
  const bin = path.join(root, 'fake-bin');
  await mkdir(bin);
  const trace = path.join(root, 'commands.log');
  await makeProviderProject(root, 'cloudflare');
  await fakeCli(bin, 'wrangler', wranglerCli);
  const env = {
    ...process.env,
    PATH: `${bin}${path.delimiter}${process.env.PATH}`,
    DOTDEPLOYER_WRANGLER_TRACE: trace,
  };

  const verify = spawnSync(process.execPath, ['./bin/dotdeployer.js', 'verify', root, '--provider', 'cloudflare', '--json'], {
    cwd: repositoryRoot,
    encoding: 'utf8',
    env,
  });
  assert.equal(verify.status, 0, verify.stderr);
  assert.equal(JSON.parse(verify.stdout).authentication.authenticated, true);
  assert.equal(verify.stdout.includes(identitySentinel), false);

  const simulation = spawnSync(process.execPath, [
    './bin/dotdeployer.js', 'deploy', root, '--provider', 'cloudflare', '--environment', 'staging', '--json',
  ], { cwd: repositoryRoot, encoding: 'utf8', env });
  assert.equal(simulation.status, 0, simulation.stderr);
  assert.equal(JSON.parse(simulation.stdout).deployed, false);

  const unsafe = spawnSync(process.execPath, [
    './bin/dotdeployer.js', 'deploy', root, '--provider', 'cloudflare', '--environment', 'staging', '--execute', '--json',
  ], { cwd: repositoryRoot, encoding: 'utf8', env });
  assert.equal(unsafe.status, 2);
  assert.equal(unsafe.stderr, '');
  const usageError = JSON.parse(unsafe.stdout);
  assert.equal(usageError.error.code, 'USAGE_ERROR');
  assert.match(usageError.error.message, /requires both --execute and --yes/);
  const calls = await readFile(trace, 'utf8');
  assert.doesNotMatch(calls, /^deploy --env staging$/m);
});

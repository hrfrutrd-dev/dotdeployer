import test from 'node:test';
import assert from 'node:assert/strict';
import { chmod, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { emulateProject } from '../../src/commands/emulation.js';

async function makeProject(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'dotdeployer-emulator-test-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  return root;
}

async function write(root, relative, text) {
  const target = path.join(root, relative);
  await mkdir(path.dirname(target), { recursive: true });
  await writeFile(target, text);
}

test('uses Wrangler official dry-run and preserves its runner version in the report', async (t) => {
  const root = await makeProject(t);
  const bin = path.join(root, 'fake-bin');
  await mkdir(bin);
  await write(root, 'wrangler.toml', 'name = "local-worker"\nmain = "src/index.js"\ncompatibility_date = "2026-10-01"\n');
  await write(root, 'src/index.js', 'export default { fetch() { return new Response("ok"); } };');
  await write(bin, 'wrangler', `#!/bin/sh
if [ "$1" = "--version" ]; then echo 'wrangler 4.39.0'; exit 0; fi
if [ "$1" = "deploy" ] && [ "$2" = "--dry-run" ]; then mkdir -p "$4"; echo 'dry-run compiled'; exit 0; fi
exit 42
`);
  await chmod(path.join(bin, 'wrangler'), 0o755);

  const env = { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH}` };
  const result = await emulateProject({ projectRoot: root, provider: 'cloudflare', env, timeoutSeconds: 10 });
  assert.equal(result.ok, true);
  assert.equal(result.emulationLevel, 'official-wrangler-dry-run');
  assert.ok(result.limitations.some((item) => item.includes('quotas')));
  assert.match(result.runner.version, /4\.39\.0/);
  assert.ok(result.commands.some((command) => command.name.includes('deploy --dry-run --outdir')));
});

test('installs locked Worker dependencies only when explicitly requested', async (t) => {
  const root = await makeProject(t);
  const bin = path.join(root, 'fake-bin');
  await mkdir(bin);
  await write(root, 'wrangler.toml', 'name = "local-worker"\nmain = "src/index.js"\ncompatibility_date = "2026-10-01"\n');
  await write(root, 'src/index.js', 'export default {};');
  await write(root, 'package.json', JSON.stringify({ name: 'worker', version: '1.0.0' }));
  await write(root, 'package-lock.json', JSON.stringify({ name: 'worker', version: '1.0.0', lockfileVersion: 3, packages: { '': { name: 'worker', version: '1.0.0' } } }));
  await write(bin, 'npm', '#!/bin/sh\necho locked install complete\n');
  await write(bin, 'wrangler', `#!/bin/sh
if [ "$1" = "--version" ]; then echo 'wrangler 4.39.0'; exit 0; fi
if [ "$1" = "deploy" ] && [ "$2" = "--dry-run" ]; then mkdir -p "$4"; exit 0; fi
exit 42
`);
  await chmod(path.join(bin, 'npm'), 0o755);
  await chmod(path.join(bin, 'wrangler'), 0o755);

  const env = { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH}` };
  const result = await emulateProject({ projectRoot: root, provider: 'cloudflare', env, install: true, timeoutSeconds: 10 });
  assert.equal(result.ok, true);
  assert.ok(result.commands.some((command) => command.name === 'npm ci --no-audit --no-fund'));
});

test('rejects an oversized Wrangler dry-run artifact', async (t) => {
  const root = await makeProject(t);
  const bin = path.join(root, 'fake-bin');
  await mkdir(bin);
  await write(root, 'wrangler.toml', 'name = "local-worker"\nmain = "src/index.js"\ncompatibility_date = "2026-10-01"\n');
  await write(root, 'src/index.js', 'export default {};');
  await write(bin, 'wrangler', `#!/bin/sh
if [ "$1" = "--version" ]; then echo 'wrangler 4.39.0'; exit 0; fi
if [ "$1" = "deploy" ] && [ "$2" = "--dry-run" ]; then node -e 'require("fs").mkdirSync(process.argv[1], { recursive: true }); require("fs").writeFileSync(process.argv[1] + "/large.bin", Buffer.alloc(20000000))' "$4"; exit 0; fi
exit 42
`);
  await chmod(path.join(bin, 'wrangler'), 0o755);

  const env = { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH}` };
  const result = await emulateProject({ projectRoot: root, provider: 'cloudflare', env, timeoutSeconds: 10 });
  assert.equal(result.ok, false);
  assert.ok(result.issues.some((item) => item.code === 'FILE_TOO_LARGE'));
});

test('fails closed when the required official provider CLI is missing', async (t) => {
  const root = await makeProject(t);
  await write(root, 'wrangler.toml', 'name = "local-worker"\nmain = "src/index.js"\ncompatibility_date = "2026-10-01"\n');
  await write(root, 'src/index.js', 'export default {};');

  const result = await emulateProject({ projectRoot: root, provider: 'cloudflare', env: { ...process.env, PATH: '' }, timeoutSeconds: 5 });
  assert.equal(result.ok, false);
  assert.ok(result.issues.some((item) => item.code === 'OFFICIAL_CLI_MISSING'));
});

test('runs a Cloudflare Pages local build without claiming a hosted runtime', async (t) => {
  const root = await makeProject(t);
  await write(root, 'wrangler.toml', [
    'name = "local-pages"',
    'compatibility_date = "2026-10-01"',
    'pages_build_output_dir = "public"',
    '',
    '[build]',
    'command = "mkdir -p public"',
  ].join('\n'));

  const result = await emulateProject({ projectRoot: root, provider: 'cloudflare', timeoutSeconds: 10 });
  assert.equal(result.ok, true);
  assert.equal(result.emulationLevel, 'local-pages-build-only');
  assert.equal(result.runner, undefined);
  assert.ok(result.issues.some((item) => item.code === 'PAGES_RUNTIME_NOT_EMULATED'));
});

test('rejects oversized Vercel build output', async (t) => {
  const root = await makeProject(t);
  const bin = path.join(root, 'fake-bin');
  await mkdir(bin);
  await write(root, 'vercel.json', '{}');
  await write(root, '.vercel/project.json', JSON.stringify({ projectId: 'prj_test', orgId: 'team_test' }));
  await write(bin, 'vercel', `#!/bin/sh
if [ "$1" = "--version" ]; then echo 'Vercel CLI 49.0.0'; exit 0; fi
if [ "$1" = "build" ]; then node -e 'require("fs").mkdirSync(".vercel/output", { recursive: true }); require("fs").writeFileSync(".vercel/output/large.bin", Buffer.alloc(20000000))'; exit 0; fi
exit 42
`);
  await chmod(path.join(bin, 'vercel'), 0o755);

  const env = { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH}` };
  const result = await emulateProject({ projectRoot: root, provider: 'vercel', env, timeoutSeconds: 10 });
  assert.equal(result.ok, false);
  assert.ok(result.issues.some((item) => item.code === 'FILE_TOO_LARGE'));
});

test('does not claim a local Apps Script runtime exists', async (t) => {
  const root = await makeProject(t);
  await write(root, 'appsscript.json', JSON.stringify({ timeZone: 'Etc/UTC', runtimeVersion: 'V8' }));
  await write(root, 'Code.gs', 'function hello() { return "ok"; }');

  const result = await emulateProject({ projectRoot: root, provider: 'gas' });
  assert.equal(result.ok, false);
  assert.ok(result.issues.some((item) => item.code === 'GAS_LOCAL_RUNTIME_UNAVAILABLE'));
});

test('runs assets-only Workers from a nested Wrangler config directory', async (t) => {
  const root = await makeProject(t);
  const bin = path.join(root, 'fake-bin');
  const trace = path.join(root, 'wrangler.cwd');
  await mkdir(bin);
  await write(root, 'apps/worker/wrangler.toml', [
    'name = "assets-only-worker"',
    'compatibility_date = "2026-10-01"',
    '',
    '[assets]',
    'directory = "public"',
  ].join('\n'));
  await write(root, 'apps/worker/public/index.html', '<h1>assets</h1>');
  await write(bin, 'wrangler', `#!/bin/sh
if [ "$1" = "--version" ]; then echo 'wrangler 4.99.0'; exit 0; fi
if [ "$1" = "deploy" ] && [ "$2" = "--dry-run" ]; then pwd > "$DOTDEPLOYER_WRANGLER_CWD"; mkdir -p "$4"; exit 0; fi
exit 42
`);
  await chmod(path.join(bin, 'wrangler'), 0o755);

  const env = {
    ...process.env,
    PATH: `${bin}${path.delimiter}${process.env.PATH}`,
    DOTDEPLOYER_WRANGLER_CWD: trace,
  };
  const result = await emulateProject({ projectRoot: root, provider: 'cloudflare', env, timeoutSeconds: 10 });
  assert.equal(result.ok, true, JSON.stringify(result.issues));
  assert.equal(result.emulationLevel, 'official-wrangler-dry-run');
  assert.ok(result.commands.some((command) => command.name.includes('deploy --dry-run --outdir')));
  const invocationDirectory = (await readFile(trace, 'utf8')).trim();
  assert.ok(invocationDirectory.endsWith(path.join('apps', 'worker')), invocationDirectory);
});

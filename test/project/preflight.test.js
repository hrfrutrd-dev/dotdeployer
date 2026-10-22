import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtemp, mkdir, rm, symlink, truncate, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { checkProject } from '../../src/commands/check.js';
import { copyDeployContext, runLocalBuild } from '../../src/commands/build.js';
import { scanProjectFiles } from '../../src/project/files.js';

async function makeProject() {
  const root = await mkdtemp(path.join(os.tmpdir(), 'dotdeployer-test-'));
  return root;
}

async function write(root, relative, content) {
  const target = path.join(root, relative);
  await mkdir(path.dirname(target), { recursive: true });
  await writeFile(target, content);
}

test('rejects a file at the 20 MB threshold before parsing malformed provider config', async (t) => {
  const root = await makeProject();
  t.after(() => rm(root, { recursive: true, force: true }));
  await write(root, 'vercel.json', '{ definitely not json');
  const large = path.join(root, 'assets', 'upload.bin');
  await mkdir(path.dirname(large), { recursive: true });
  await writeFile(large, '');
  await truncate(large, 20_000_000);

  const result = await checkProject({ projectRoot: root, provider: 'vercel' });
  assert.equal(result.ok, false);
  assert.equal(result.stoppedBeforeConfigValidation, true);
  assert.equal(result.issues[0].code, 'FILE_TOO_LARGE');
  assert.match(result.issues[0].message, /20 MB/);
  assert.equal(result.issues.some((item) => item.code === 'CONFIG_PARSE_ERROR'), false);
});

test('validates common Vercel config and reports missing lockfile as a warning', async (t) => {
  const root = await makeProject();
  t.after(() => rm(root, { recursive: true, force: true }));
  await write(root, 'package.json', JSON.stringify({ name: 'sample', scripts: { build: 'echo build' } }));
  await write(root, 'vercel.json', JSON.stringify({
    $schema: 'https://openapi.vercel.sh/vercel.json',
    outputDirectory: 'public',
    rewrites: [{ source: '/(.*)', destination: '/index.html' }],
  }));
  await mkdir(path.join(root, 'public'));

  const result = await checkProject({ projectRoot: root, provider: 'vercel', plan: 'hobby' });
  assert.equal(result.ok, true);
  assert.equal(result.provider, 'vercel');
  assert.ok(result.issues.some((item) => item.code === 'LOCKFILE_MISSING' && item.severity === 'warning'));
});

test('rejects a Render persistent disk under the free profile with an actionable error', async (t) => {
  const root = await makeProject();
  t.after(() => rm(root, { recursive: true, force: true }));
  await write(root, 'render.yaml', [
    'services:',
    '  - type: web',
    '    name: api',
    '    plan: free',
    '    buildCommand: npm run build',
    '    startCommand: npm start',
    '    disk:',
    '      name: state',
    '      mountPath: /var/data',
    '      sizeGB: 1',
  ].join('\n'));

  const result = await checkProject({ projectRoot: root, provider: 'render', plan: 'free' });
  assert.equal(result.ok, false);
  assert.ok(result.issues.some((item) => item.code === 'PLAN_LIMIT_DISK' && item.severity === 'error'));
});

test('parses Railway TOML and validates deploy settings', async (t) => {
  const root = await makeProject();
  t.after(() => rm(root, { recursive: true, force: true }));
  await write(root, 'railway.toml', [
    '[build]',
    'builder = "RAILPACK"',
    'buildCommand = "npm run build"',
    '',
    '[deploy]',
    'startCommand = "npm start"',
    'healthcheckPath = "/health"',
    'healthcheckTimeout = 30',
    'restartPolicyType = "ON_FAILURE"',
  ].join('\n'));

  const result = await checkProject({ projectRoot: root, provider: 'railway', plan: 'free' });
  assert.equal(result.ok, true);
  assert.equal(result.provider, 'railway');
});

test('validates Apps Script manifest under clasp rootDir', async (t) => {
  const root = await makeProject();
  t.after(() => rm(root, { recursive: true, force: true }));
  await write(root, '.clasp.json', JSON.stringify({ scriptId: 'fake-script-id', rootDir: 'src' }));
  await write(root, 'src/appsscript.json', JSON.stringify({
    timeZone: 'America/Los_Angeles',
    runtimeVersion: 'V8',
    exceptionLogging: 'STACKDRIVER',
    oauthScopes: ['https://www.googleapis.com/auth/script.projects'],
  }));
  await write(root, 'src/Code.gs', 'function hello() { return "ok"; }');

  const result = await checkProject({ projectRoot: root, provider: 'gas', plan: 'free' });
  assert.equal(result.ok, true);
  assert.equal(result.detectedProviders[0], 'gas');
});

test('parses JSONC Wrangler config and requires an explicit compatibility date', async (t) => {
  const root = await makeProject();
  t.after(() => rm(root, { recursive: true, force: true }));
  await write(root, 'wrangler.jsonc', `{
    // Keep the runtime pinned.
    "name": "example-worker",
    "main": "src/index.js",
    "compatibility_date": "2026-10-01",
    "vars": { "MODE": "test" },
  }`);
  await write(root, 'src/index.js', 'export default { fetch() { return new Response("ok"); } };');

  const result = await checkProject({ projectRoot: root, provider: 'cloudflare' });
  assert.equal(result.ok, true);
  assert.equal(result.detectedProviders[0], 'cloudflare');
});

test('reports the missing Wrangler compatibility date as a hard error', async (t) => {
  const root = await makeProject();
  t.after(() => rm(root, { recursive: true, force: true }));
  await write(root, 'wrangler.toml', 'name = "no-date"\nmain = "src/index.js"\n');
  await write(root, 'src/index.js', 'export default {};');

  const result = await checkProject({ projectRoot: root, provider: 'cloudflare' });
  assert.equal(result.ok, false);
  assert.ok(result.issues.some((item) => item.code === 'CLOUDFLARE_COMPATIBILITY_DATE_REQUIRED'));
});

test('runs a package build in an isolated temporary copy', async (t) => {
  const root = await makeProject();
  t.after(() => rm(root, { recursive: true, force: true }));
  await write(root, 'package.json', JSON.stringify({
    name: 'build-sample',
    scripts: { build: 'node build.js' },
  }));
  await write(root, 'build.js', "require('node:fs').mkdirSync('dist', { recursive: true }); console.log('build completed');");

  const result = await runLocalBuild({ projectRoot: root, provider: 'generic', timeoutSeconds: 20 });
  assert.equal(result.preflight.ok, true);
  assert.equal(result.attempted, true);
  assert.equal(result.success, true);
  assert.equal(result.commands[0].exitCode, 0);
  assert.match(result.commands[0].stdout, /build completed/);
  assert.equal(result.workDirectory, undefined);
});

test('strict build mode allows a static output directory to be generated by the build', async (t) => {
  const root = await makeProject();
  t.after(() => rm(root, { recursive: true, force: true }));
  await write(root, 'render.yaml', [
    'services:',
    '  - type: static',
    '    name: site',
    '    buildCommand: mkdir -p public',
    '    staticPublishPath: public',
  ].join('\n'));

  const result = await runLocalBuild({ projectRoot: root, provider: 'render', strict: true, timeoutSeconds: 20 });
  assert.equal(result.preflight.ok, true);
  assert.equal(result.attempted, true);
  assert.equal(result.success, true);
  assert.equal(result.commands[0].exitCode, 0);
});

test('rejects oversized generated deployment artifacts after build', async (t) => {
  const root = await makeProject();
  t.after(() => rm(root, { recursive: true, force: true }));
  await write(root, 'render.yaml', [
    'services:',
    '  - type: static',
    '    name: site',
    '    buildCommand: node build.js',
    '    staticPublishPath: public',
  ].join('\n'));
  await write(root, 'build.js', "require('node:fs').mkdirSync('public', { recursive: true }); require('node:fs').writeFileSync('public/large.bin', Buffer.alloc(20_000_000));");

  const result = await runLocalBuild({ projectRoot: root, provider: 'render', timeoutSeconds: 20 });
  assert.equal(result.preflight.ok, true);
  assert.equal(result.success, false);
  assert.ok(result.issues.some((item) => item.code === 'FILE_TOO_LARGE'));
});

test('reports malformed package metadata without throwing during preflight', async (t) => {
  const cases = [
    {
      packageJson: { name: 'invalid-metadata', engines: '>=20', packageManager: 42 },
      expected: ['PACKAGE_ENGINES_INVALID', 'PACKAGE_MANAGER_INVALID'],
    },
    {
      packageJson: { name: 'invalid-metadata', engines: { node: '' }, packageManager: '' },
      expected: ['PACKAGE_NODE_ENGINE_INVALID', 'PACKAGE_MANAGER_INVALID'],
    },
  ];

  for (const { packageJson, expected } of cases) {
    const root = await makeProject();
    t.after(() => rm(root, { recursive: true, force: true }));
    await write(root, 'package.json', JSON.stringify(packageJson));

    const result = await checkProject({ projectRoot: root, provider: 'generic' });
    assert.equal(result.ok, false);
    for (const code of expected) assert.ok(result.issues.some((item) => item.code === code), `missing ${code}`);
  }
});

test('accepts valid Node engine and package manager metadata with its lockfile', async (t) => {
  const root = await makeProject();
  t.after(() => rm(root, { recursive: true, force: true }));
  await write(root, 'package.json', JSON.stringify({
    name: 'valid-metadata',
    engines: { node: '>=20' },
    packageManager: 'npm@10.9.8',
  }));
  await write(root, 'package-lock.json', JSON.stringify({ name: 'valid-metadata', lockfileVersion: 3, packages: {} }));

  const result = await checkProject({ projectRoot: root, provider: 'generic' });
  assert.equal(result.ok, true);
  assert.equal(result.issues.some((item) => item.code.startsWith('PACKAGE_')), false);
});

test('strict mode promotes reproducibility warnings to errors', async (t) => {
  const root = await makeProject();
  t.after(() => rm(root, { recursive: true, force: true }));
  await write(root, 'package.json', JSON.stringify({ name: 'no-lock', scripts: {} }));

  const result = await checkProject({ projectRoot: root, provider: 'generic', strict: true });
  assert.equal(result.ok, false);
  assert.ok(result.issues.some((item) => item.code === 'LOCKFILE_MISSING' && item.severity === 'error'));
});

test('respects .gitignore when measuring the Git deployment context', async (t) => {
  const root = await makeProject();
  t.after(() => rm(root, { recursive: true, force: true }));
  execFileSync('git', ['init', '-q'], { cwd: root });
  execFileSync('git', ['config', 'user.email', 'test@example.invalid'], { cwd: root });
  execFileSync('git', ['config', 'user.name', 'dotdeployer test'], { cwd: root });
  await write(root, '.gitignore', 'ignored.bin\n');
  await write(root, 'package.json', '{"name":"test"}');
  const ignored = path.join(root, 'ignored.bin');
  await writeFile(ignored, '');
  await truncate(ignored, 20_000_000);
  execFileSync('git', ['add', '.gitignore', 'package.json'], { cwd: root });
  execFileSync('git', ['commit', '-qm', 'test files'], { cwd: root });

  const result = await checkProject({ projectRoot: root, provider: 'generic' });
  assert.equal(result.ok, true);
  assert.equal(result.filesScanned, 2);
});

test('validates Cloudflare environment overrides with environment-specific field paths', async (t) => {
  const root = await makeProject();
  t.after(() => rm(root, { recursive: true, force: true }));
  await write(root, 'wrangler.toml', [
    'name = "environment-worker"',
    'main = "src/index.js"',
    'compatibility_date = "2026-10-01"',
    '',
    '[env.staging]',
    'main = "src/missing.js"',
    'compatibility_date = "2026-02-30"',
    'vars = { API_TOKEN = 123 }',
    'assets = { directory = 42 }',
  ].join('\n'));
  await write(root, 'src/index.js', 'export default {};');

  const result = await checkProject({ projectRoot: root, provider: 'cloudflare' });
  assert.equal(result.ok, false);
  assert.ok(result.issues.some((item) => item.code === 'CONFIG_PATH_NOT_FOUND' && item.message.includes('env.staging.main')));
  assert.ok(result.issues.some((item) => item.code === 'CLOUDFLARE_COMPATIBILITY_DATE_INVALID' && item.message.includes('env.staging.compatibility_date')));
  assert.ok(result.issues.some((item) => item.code === 'CLOUDFLARE_VAR_INVALID' && item.message.includes('env.staging.vars.API_TOKEN')));
  assert.ok(result.issues.some((item) => item.code === 'CONFIG_FIELD_TYPE' && item.message.includes('env.staging.assets.directory')));
});

import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { auditProject } from '../../src/commands/audit.js';
import { reportToSarif } from '../../src/reporting/sarif.js';

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');

async function makeProject(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'dotdeployer-audit-test-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  return root;
}

async function write(root, relative, content) {
  const target = path.join(root, relative);
  await mkdir(path.dirname(target), { recursive: true });
  await writeFile(target, content);
}

const serverEnvironment = (name) => `process.env.${name}`;
const clientEnvironment = (name) => `import.meta.env.${name}`;

test('finds a committed credential without returning its value and emits safe SARIF', async (t) => {
  const root = await makeProject(t);
  const token = `ghp_${'A'.repeat(36)}`;
  await write(root, 'src/config.js', `export const token = "${token}";\n`);

  const report = await auditProject({ projectRoot: root, provider: 'generic' });
  assert.equal(report.ok, false);
  const finding = report.auditIssues.find((item) => item.code === 'SECRET_GITHUB_TOKEN');
  assert.ok(finding);
  assert.equal(finding.file, 'src/config.js');
  assert.equal(finding.line, 1);
  assert.equal(JSON.stringify(report).includes(token), false);

  const sarif = reportToSarif(report);
  const sarifResult = sarif.runs[0].results.find((item) => item.ruleId === 'SECRET_GITHUB_TOKEN');
  assert.equal(sarifResult.locations[0].physicalLocation.artifactLocation.uri, 'src/config.js');
  assert.equal(sarifResult.locations[0].physicalLocation.region.startLine, 1);
  assert.equal('help' in sarifResult, false);
  assert.ok(sarif.runs[0].tool.driver.rules.find((rule) => rule.id === 'SECRET_GITHUB_TOKEN').help.text);
  assert.equal(JSON.stringify(sarif).includes(token), false);
});

test('CLI emits secret-safe SARIF and returns a failing status for audit findings', async (t) => {
  const root = await makeProject(t);
  const token = `ghp_${'B'.repeat(36)}`;
  await write(root, 'src/config.js', `export const token = "${token}";\n`);

  const result = spawnSync(process.execPath, ['./bin/dotdeployer.js', 'audit', root, '--provider', 'generic', '--sarif'], {
    cwd: repositoryRoot,
    encoding: 'utf8',
  });
  assert.equal(result.status, 1);
  assert.equal(result.stderr, '');
  assert.equal(result.stdout.includes(token), false);
  const sarif = JSON.parse(result.stdout);
  assert.equal(sarif.version, '2.1.0');
  assert.ok(sarif.runs[0].results.some((item) => item.ruleId === 'SECRET_GITHUB_TOKEN'));
});

test('detects a private key in an extensionless text file without emitting its contents', async (t) => {
  const root = await makeProject(t);
  const privateKeyHeader = ['-----BEGIN', 'PRIVATE KEY-----'].join(' ');
  await write(root, 'keys/deploy-credential', `${privateKeyHeader}\nnot-a-real-key\n-----END PRIVATE KEY-----\n`);

  const report = await auditProject({ projectRoot: root, provider: 'generic' });
  const finding = report.auditIssues.find((item) => item.code === 'SECRET_PRIVATE_KEY');
  assert.ok(finding);
  assert.equal(finding.file, 'keys/deploy-credential');
  assert.equal(JSON.stringify(report).includes(privateKeyHeader), false);
});

test('builds an environment contract from source, templates, and provider configuration', async (t) => {
  const root = await makeProject(t);
  await write(root, '.env.example', 'DATABASE_URL=\n');
  await write(root, 'vercel.json', JSON.stringify({ env: { API_URL: 'https://api.example.test' } }));
  await write(root, 'src/server.ts', [
    `export const database = ${serverEnvironment('DATABASE_URL')};`,
    `export const api = ${serverEnvironment('API_URL')};`,
  ].join('\n'));

  const report = await auditProject({ projectRoot: root, provider: 'generic' });
  assert.equal(report.ok, true);
  assert.equal(report.auditIssues.some((item) => item.code === 'ENVIRONMENT_VARIABLE_UNDOCUMENTED'), false);
  const database = report.environment.variables.find((item) => item.name === 'DATABASE_URL');
  const api = report.environment.variables.find((item) => item.name === 'API_URL');
  assert.equal(database.documented, true);
  assert.equal(database.configured, false);
  assert.equal(api.documented, false);
  assert.equal(api.configured, true);
  assert.equal(api.configuredIn[0].file, 'vercel.json');
});

test('fails an audit when a sensitive variable is exposed to client-side code', async (t) => {
  const root = await makeProject(t);
  await write(root, '.env.example', 'VITE_DATABASE_URL=\n');
  await write(root, 'src/client.ts', `export const url = ${clientEnvironment('VITE_DATABASE_URL')};\n`);

  const report = await auditProject({ projectRoot: root, provider: 'generic' });
  assert.equal(report.ok, false);
  const finding = report.auditIssues.find((item) => item.code === 'SENSITIVE_ENVIRONMENT_EXPOSED_TO_CLIENT');
  assert.ok(finding);
  assert.equal(finding.file, 'src/client.ts');
  assert.equal(finding.line, 1);
});

test('reports undocumented server environment references and promotes them in strict mode', async (t) => {
  const root = await makeProject(t);
  await write(root, 'src/server.js', `export const endpoint = ${serverEnvironment('INTERNAL_ENDPOINT')};\n`);

  const normal = await auditProject({ projectRoot: root, provider: 'generic' });
  const normalIssue = normal.auditIssues.find((item) => item.code === 'ENVIRONMENT_VARIABLE_UNDOCUMENTED');
  assert.equal(normal.ok, true);
  assert.equal(normalIssue.severity, 'warning');

  const strict = await auditProject({ projectRoot: root, provider: 'generic', strict: true });
  const strictIssue = strict.auditIssues.find((item) => item.code === 'ENVIRONMENT_VARIABLE_UNDOCUMENTED');
  assert.equal(strict.ok, false);
  assert.equal(strictIssue.severity, 'error');
});

test('recognizes protected npm token references but flags literal .npmrc tokens', async (t) => {
  const root = await makeProject(t);
  await write(root, '.npmrc', '//registry.npmjs.org/:_authToken=${NPM_TOKEN}\n');
  const protectedReport = await auditProject({ projectRoot: root, provider: 'generic' });
  assert.equal(protectedReport.auditIssues.some((item) => item.code === 'SECRET_NPM_TOKEN'), false);

  await write(root, '.npmrc', '//registry.npmjs.org/:_authToken=not-a-real-token-but-still-a-literal\n');
  const literalReport = await auditProject({ projectRoot: root, provider: 'generic' });
  assert.equal(literalReport.ok, false);
  assert.ok(literalReport.auditIssues.some((item) => item.code === 'SECRET_NPM_TOKEN'));
});

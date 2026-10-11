import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const entrypoint = fileURLToPath(new URL('../../bin/dotdeployer.js', import.meta.url));

function runCli(args, cwd = process.cwd()) {
  return spawnSync(process.execPath, [entrypoint, ...args], {
    cwd,
    encoding: 'utf8',
    timeout: 10_000,
  });
}

test('global help, command help, and version have standard CLI exit behavior', () => {
  const noArgs = runCli([]);
  assert.equal(noArgs.status, 0);
  assert.match(noArgs.stdout, /Usage:/);
  assert.match(noArgs.stdout, /dotdeployer <command> --help/);

  const checkHelp = runCli(['check', '--help']);
  assert.equal(checkHelp.status, 0);
  assert.match(checkHelp.stdout, /dotdeployer check \[path\]/);
  assert.match(checkHelp.stdout, /--provider <name>/);
  assert.match(checkHelp.stdout, /--repo <owner\/repo\|URL>/);
  assert.doesNotMatch(checkHelp.stdout, /Commands:/);

  const topicHelp = runCli(['help', 'deploy']);
  assert.equal(topicHelp.status, 0);
  assert.match(topicHelp.stdout, /--execute/);
  assert.match(topicHelp.stdout, /--yes/);

  const version = runCli(['--version']);
  assert.equal(version.status, 0);
  assert.match(version.stdout.trim(), /^\d+\.\d+\.\d+$/);
});

test('unknown commands and options fail with exit code 2', () => {
  const unknownCommand = runCli(['not-a-command']);
  assert.equal(unknownCommand.status, 2);
  assert.match(unknownCommand.stderr, /Unknown command: not-a-command/);
  assert.match(unknownCommand.stderr, /Usage:/);

  const incompatibleOption = runCli(['providers', '--provider', 'vercel']);
  assert.equal(incompatibleOption.status, 2);
  assert.match(incompatibleOption.stderr, /only available with/);
});

test('usage errors remain machine-readable when --json was requested', () => {
  const result = runCli(['check', '--repo', 'owner/repo', '--path', '.', '--json']);
  assert.equal(result.status, 2);
  assert.equal(result.stderr, '');
  const report = JSON.parse(result.stdout);
  assert.equal(report.ok, false);
  assert.equal(report.error.code, 'USAGE_ERROR');
  assert.match(report.error.message, /--repo cannot be combined with --path/);
});

test('providers --json returns valid machine-readable output', () => {
  const result = runCli(['providers', '--json']);
  assert.equal(result.status, 0);
  const report = JSON.parse(result.stdout);
  assert.ok(Array.isArray(report.providers));
  assert.ok(report.providers.some((provider) => provider.id === 'cloudflare'));
  assert.equal(report.fileSizeLimitBytes, 20_000_000);
});

test('check emits a single JSON report and returns its report status', async () => {
  const project = await mkdtemp(path.join(os.tmpdir(), 'dotdeployer-cli-test-'));
  try {
    const result = runCli(['check', project, '--provider', 'generic', '--json']);
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stderr, '');
    const report = JSON.parse(result.stdout);
    assert.equal(report.ok, true);
    assert.equal(report.provider, 'generic');
    assert.equal(report.projectRoot, project);
  } finally {
    await rm(project, { recursive: true, force: true });
  }
});

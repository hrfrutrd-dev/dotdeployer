import assert from 'node:assert/strict';
import test from 'node:test';
import { CliUsageError, parseArgs } from '../../src/cli/args.js';
import { normalizeProvider } from '../../src/providers/index.js';

const parseError = (args, expression) => {
  assert.throws(() => parseArgs(args), (error) => {
    assert.ok(error instanceof CliUsageError);
    assert.match(error.message, expression);
    return true;
  });
};

test('defaults to global help and supports help topics', () => {
  assert.equal(parseArgs([]).help, true);
  assert.equal(parseArgs(['--help']).help, true);
  assert.equal(parseArgs(['help']).help, true);
  assert.deepEqual(
    { command: parseArgs(['help', 'audit']).command, topic: parseArgs(['help', 'audit']).helpTopic },
    { command: 'audit', topic: 'audit' },
  );
  parseError(['help', 'not-a-command'], /Unknown help topic/);
});

test('parses project paths, flags, provider aliases, and command-specific defaults', () => {
  const check = parseArgs(['check', '--path', './project', '--provider', 'workers', '--strict', '--build', '--timeout', '45']);
  assert.equal(check.projectRoot, './project');
  assert.equal(check.provider, 'cloudflare');
  assert.equal(check.pathProvided, true);
  assert.equal(check.strict, true);
  assert.equal(check.build, true);
  assert.equal(check.timeoutSeconds, 45);

  const verify = parseArgs(['verify', '.', '--provider', 'google-apps-script']);
  assert.equal(verify.projectRoot, '.');
  assert.equal(verify.provider, 'gas');
  assert.equal(verify.timeoutSeconds, 60);

  const deploy = parseArgs(['deploy', '--env', 'staging', '--execute', '-y']);
  assert.equal(deploy.environment, 'staging');
  assert.equal(deploy.execute, true);
  assert.equal(deploy.yes, true);
});

test('validates path and repository combinations', () => {
  assert.equal(parseArgs(['check', 'project', '--provider', 'generic']).projectRoot, 'project');
  assert.equal(parseArgs(['check', '--repo', 'owner/repo', '--ref', 'feature/branch']).repository, 'owner/repo');
  parseError(['check', 'project', '--path', 'other'], /either a positional path or --path/);
  parseError(['check', '--repo', 'owner/repo', '--path', 'project'], /--repo cannot be combined with --path/);
  parseError(['check', 'project', '--repo', 'owner/repo'], /--repo cannot be combined with a positional project path/);
  parseError(['check', '--ref', 'main'], /--ref requires --repo/);
  parseError(['deploy', '--repo', 'owner/repo'], /--repo is only available/);
});

test('rejects command-incompatible options and unsafe or ambiguous combinations', () => {
  parseError(['providers', '--provider', 'vercel'], /only available with/);
  parseError(['mcp', '--json'], /only available with/);
  parseError(['audit', '--build'], /only available with check, test/);
  parseError(['check', '--install'], /--install requires --build/);
  parseError(['deploy', '--execute'], /requires both --execute and --yes/);
  parseError(['deploy', '--yes'], /--yes requires --execute/);
  parseError(['check', '--json', '--sarif'], /either --json or --sarif/);
  parseError(['check', '--timeout', '0'], /between 1 and 1800/);
  parseError(['check', '--unknown'], /Unknown option/);
  parseError(['typo'], /Unknown command/);
});

test('help is available before command option validation', () => {
  const options = parseArgs(['deploy', '--execute', '--help']);
  assert.equal(options.help, true);
  assert.equal(options.execute, true);
});

test('keeps prototype-like provider names as strings for normal unknown-provider handling', () => {
  for (const provider of ['constructor', '__proto__', 'toString']) {
    const parsed = parseArgs(['check', '--provider', provider]).provider;
    assert.equal(parsed, provider.toLowerCase());
    assert.equal(normalizeProvider(parsed), undefined);
  }
});

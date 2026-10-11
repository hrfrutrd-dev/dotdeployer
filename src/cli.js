import path from 'node:path';
import { VERSION, PROVIDERS, PROVIDER_ALIASES, MAX_FILE_SIZE_BYTES } from './constants.js';
import { checkProject } from './check.js';
import { runLocalBuild } from './build.js';
import { emulateProject } from './emulation.js';
import { providerInfo } from './providers/index.js';
import { withProjectSource } from './source.js';

const USAGE = `dotdeployer ${VERSION} — strict local deployment preflight

Usage:
  dotdeployer check [path] [options]
  dotdeployer test [path] [options]
  dotdeployer emulate [path] --provider <name> [options]
  dotdeployer providers [--json]
  dotdeployer mcp

Commands:
  check       Validate the deployable project context; never contacts a hosting API.
  test        Same preflight; optionally run a build command in a temporary copy.
  emulate     Use the provider's official local build/validation CLI where available; never deploy.
  providers   List provider adapters and their supported config formats.
  mcp         Start the MCP server over stdio for Claude Code and other agents.

Options:
  --path <dir>                Local project directory (default: .)
  --repo <owner/repo|URL>     Clone a GitHub repository for the check
  --ref <branch-or-tag>       Git ref to clone (requires --repo)
  --provider <name>           auto, all, render, railway, vercel, cloudflare, gas
  --plan <name>               Assumed plan profile (default: free; paid is also accepted)
  --strict                    Treat every warning as an error
  --build                     Run provider/package build command in a temporary copy (check/test)
  --install                   Install from lockfile for local build / Wrangler steps; Vercel/Railpack manage installs themselves
  --timeout <seconds>         Per-command timeout for --build / emulate (default: 600; max: 1800)
  --json                      Print machine-readable JSON
  -h, --help                  Show this help
  -V, --version               Print version

The 20 MB (20,000,000-byte) per-file limit is enforced on source files before config parsing/builds; known generated outputs are rescanned after local builds.
A successful check is a conservative preflight, not a full cloud runtime emulator.`;

function parseArgs(argv) {
  const args = [...argv];
  const command = args.shift();
  const options = {
    command,
    projectRoot: '.',
    provider: 'auto',
    plan: 'free',
    strict: false,
    build: false,
    install: false,
    json: false,
    timeoutSeconds: 600,
    pathProvided: false,
  };
  const positional = [];
  while (args.length) {
    const arg = args.shift();
    if (arg === '--path') {
      options.projectRoot = requireValue(args, '--path');
      options.pathProvided = true;
    } else if (arg === '--repo') {
      options.repository = requireValue(args, '--repo');
    } else if (arg === '--ref') {
      options.ref = requireValue(args, '--ref');
    } else if (arg === '--provider') {
      options.provider = normalizeProviderOption(requireValue(args, '--provider'));
    } else if (arg === '--plan') {
      options.plan = requireValue(args, '--plan');
      if (!options.plan.trim()) throw new Error('--plan cannot be empty.');
    } else if (arg === '--timeout') {
      const value = Number(requireValue(args, '--timeout'));
      if (!Number.isInteger(value) || value < 1 || value > 1800) throw new Error('--timeout must be an integer between 1 and 1800 seconds.');
      options.timeoutSeconds = value;
    } else if (arg === '--strict') options.strict = true;
    else if (arg === '--build') options.build = true;
    else if (arg === '--install') options.install = true;
    else if (arg === '--json') options.json = true;
    else if (arg === '-h' || arg === '--help') options.help = true;
    else if (arg === '-V' || arg === '--version') options.version = true;
    else if (arg.startsWith('-')) throw new Error(`Unknown option: ${arg}`);
    else positional.push(arg);
  }

  if (positional.length > 1) throw new Error('Only one project path may be supplied.');
  if (positional.length) {
    if (options.pathProvided) throw new Error('Use either a positional path or --path, not both.');
    if (options.repository) throw new Error('--repo cannot be combined with a positional project path.');
    options.projectRoot = positional[0];
  }
  if (options.repository && options.pathProvided) throw new Error('--repo cannot be combined with --path.');
  if (options.ref && !options.repository) throw new Error('--ref requires --repo.');
  if (options.install && !options.build && command !== 'emulate') throw new Error('--install requires --build or the emulate command.');
  if (options.build && command !== 'test' && command !== 'check') throw new Error('--build is only available with check or test.');
  return options;
}

function requireValue(args, option) {
  const value = args.shift();
  if (!value || value.startsWith('--')) throw new Error(`${option} requires a value.`);
  return value;
}

function normalizeProviderOption(value) {
  const normal = value.toLowerCase();
  return PROVIDER_ALIASES[normal] ?? normal;
}

function providerLabel(provider) {
  if (Array.isArray(provider)) return provider.map((id) => PROVIDERS[id]?.label ?? id).join(', ');
  return PROVIDERS[provider]?.label ?? (provider === 'auto' ? 'auto-detect' : provider);
}

function issuePrefix(severity) {
  if (severity === 'error') return '✖';
  if (severity === 'warning') return '⚠';
  return 'ℹ';
}

function printCheckReport(report, { json = false } = {}) {
  if (json) {
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
    return;
  }
  process.stdout.write(`dotdeployer ${VERSION} — strict local deployment preflight\n`);
  process.stdout.write(`Project: ${report.projectRoot}\n`);
  process.stdout.write(`Target: ${providerLabel(report.provider)} / ${report.plan}\n`);
  process.stdout.write(`Files scanned: ${report.filesScanned} (per-file limit ${MAX_FILE_SIZE_BYTES.toLocaleString('en-US')} bytes)\n`);
  process.stdout.write(`Result: ${report.ok ? 'PASS' : 'FAIL'} — ${report.summary.errors} error(s), ${report.summary.warnings} warning(s), ${report.summary.infos} info\n`);
  for (const item of report.issues) {
    const location = item.file ? ` ${item.file}:` : '';
    process.stdout.write(`${issuePrefix(item.severity)} [${item.code}]${location} ${item.message}\n`);
    if (item.remediation) process.stdout.write(`  Fix: ${item.remediation}\n`);
  }
  process.stdout.write('\nA PASS is a local preflight, not a guarantee of cloud behavior. Provider quotas, secrets, networks, and managed runtime details may differ.\n');
}

function printBuildReport(report, { json = false } = {}) {
  if (json) {
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
    return;
  }
  printCheckReport(report.preflight);
  if (report.attempted) process.stdout.write(`\nBuild: ${report.success ? 'PASS' : 'FAIL'}\n`);
  else process.stdout.write('\nBuild: NOT RUN\n');
  for (const command of report.commands) {
    const status = command.timedOut ? 'TIMEOUT' : command.exitCode === 0 ? 'PASS' : `EXIT ${command.exitCode ?? command.signal ?? 'unknown'}`;
    process.stdout.write(`  ${status} (${(command.durationMs / 1000).toFixed(1)}s): ${command.name}\n`);
  }
  for (const item of report.issues) {
    process.stdout.write(`${issuePrefix(item.severity)} [${item.code}]${item.file ? ` ${item.file}:` : ''} ${item.message}\n`);
    if (item.remediation) process.stdout.write(`  Fix: ${item.remediation}\n`);
  }
}

function printEmulationReport(report, { json = false } = {}) {
  if (json) {
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
    return;
  }
  printCheckReport(report.preflight);
  process.stdout.write(`\nProvider runner: ${report.runner ? `${report.runner.name} ${report.runner.version}` : 'none'}\n`);
  process.stdout.write(`Parity scope: ${report.emulationLevel}\n`);
  process.stdout.write(`Emulation: ${report.ok ? 'PASS' : 'FAIL'}\n`);
  for (const command of report.commands) {
    const status = command.timedOut ? 'TIMEOUT' : command.exitCode === 0 ? 'PASS' : `EXIT ${command.exitCode ?? command.signal ?? 'unknown'}`;
    process.stdout.write(`  ${status} (${(command.durationMs / 1000).toFixed(1)}s): ${command.name}\n`);
  }
  if (report.localBuild) {
    process.stdout.write(`Local build: ${report.localBuild.success ? 'PASS' : 'FAIL'}\n`);
    for (const command of report.localBuild.commands) {
      const status = command.timedOut ? 'TIMEOUT' : command.exitCode === 0 ? 'PASS' : `EXIT ${command.exitCode ?? command.signal ?? 'unknown'}`;
      process.stdout.write(`  ${status} (${(command.durationMs / 1000).toFixed(1)}s): ${command.name}\n`);
    }
  }
  for (const item of report.issues) {
    process.stdout.write(`${issuePrefix(item.severity)} [${item.code}] ${item.message}\n`);
    if (item.remediation) process.stdout.write(`  Fix: ${item.remediation}\n`);
  }
  for (const limitation of report.limitations ?? []) process.stdout.write(`Scope limit: ${limitation}\n`);
}

function printProviders(asJson) {
  const info = providerInfo();
  if (asJson) {
    process.stdout.write(`${JSON.stringify({ providers: info, fileSizeLimitBytes: MAX_FILE_SIZE_BYTES }, null, 2)}\n`);
    return;
  }
  process.stdout.write('Supported static adapters:\n');
  for (const provider of info) process.stdout.write(`  ${provider.id.padEnd(12)} ${provider.label} — ${provider.description}\n`);
  process.stdout.write('\nFree/paid is a policy label, not a full recreation of current vendor quotas. See README.md for scope and limitations.\n');
}

export async function main(argv = process.argv.slice(2)) {
  if (!argv.length) {
    process.stdout.write(`${USAGE}\n`);
    return 0;
  }
  let options;
  try {
    options = parseArgs(argv);
  } catch (error) {
    process.stderr.write(`dotdeployer: ${error.message}\n\n${USAGE}\n`);
    return 2;
  }
  if (options.help || options.command === 'help' || options.command === '--help' || options.command === '-h') {
    process.stdout.write(`${USAGE}\n`);
    return 0;
  }
  if (options.version || options.command === '--version' || options.command === '-V') {
    process.stdout.write(`${VERSION}\n`);
    return 0;
  }
  if (options.command === 'providers') {
    printProviders(options.json);
    return 0;
  }
  if (options.command === 'mcp') {
    const { startMcpServer } = await import('./mcp.js');
    await startMcpServer();
    return 0;
  }
  if (!['check', 'test', 'emulate'].includes(options.command)) {
    process.stderr.write(`Unknown command: ${options.command}\n\n${USAGE}\n`);
    return 2;
  }

  try {
    let exitCode = 0;
    await withProjectSource({
      projectRoot: path.resolve(options.projectRoot),
      repository: options.repository,
      ref: options.ref,
    }, async (projectRoot) => {
      if (options.command === 'emulate') {
        const output = options.json ? undefined : (stream, text) => {
          (stream === 'stderr' ? process.stderr : process.stdout).write(text);
        };
        const report = await emulateProject({
          projectRoot,
          provider: options.provider,
          plan: options.plan,
          install: options.install,
          timeoutSeconds: options.timeoutSeconds,
          strict: options.strict,
          onOutput: output,
        });
        if (options.repository) {
          report.projectRoot = options.repository;
          report.preflight.projectRoot = options.repository;
          if (report.localBuild?.preflight) report.localBuild.preflight.projectRoot = options.repository;
        }
        printEmulationReport(report, { json: options.json });
        if (!report.ok) exitCode = 1;
      } else if (options.build) {
        const output = options.json ? undefined : (stream, text) => {
          (stream === 'stderr' ? process.stderr : process.stdout).write(text);
        };
        const report = await runLocalBuild({
          projectRoot,
          provider: options.provider,
          plan: options.plan,
          install: options.install,
          timeoutSeconds: options.timeoutSeconds,
          strict: options.strict,
          onOutput: output,
        });
        if (options.repository) report.preflight.projectRoot = options.repository;
        printBuildReport(report, { json: options.json });
        if (!report.preflight.ok || !report.success) exitCode = 1;
      } else {
        const report = await checkProject({
          projectRoot,
          provider: options.provider,
          plan: options.plan,
          strict: options.strict,
        });
        if (options.repository) report.projectRoot = options.repository;
        printCheckReport(report, { json: options.json });
        if (!report.ok) exitCode = 1;
      }
    });
    return exitCode;
  } catch (error) {
    if (options.json) {
      process.stdout.write(`${JSON.stringify({
        ok: false,
        error: { code: error?.code ?? 'UNEXPECTED_ERROR', message: error?.message ?? String(error) },
      }, null, 2)}\n`);
    } else {
      process.stderr.write(`dotdeployer: ${error?.code ?? 'UNEXPECTED_ERROR'}: ${error?.message ?? String(error)}\n`);
    }
    return 2;
  }
}

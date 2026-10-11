import path from 'node:path';
import { VERSION } from '../core/constants.js';
import { auditProject } from '../commands/audit.js';
import { runLocalBuild } from '../commands/build.js';
import { checkProject } from '../commands/check.js';
import { deployProject, verifyProject } from '../commands/deployment.js';
import { emulateProject } from '../commands/emulation.js';
import { reportToSarif } from '../reporting/sarif.js';
import { withProjectSource } from '../project/source.js';
import { parseArgs } from './args.js';
import { commandHelp, globalHelp } from './help.js';
import { COMMANDS } from './spec.js';
import {
  printAuditReport,
  printBuildReport,
  printCheckReport,
  printDeployReport,
  printEmulationReport,
  printProviders,
  printVerificationReport,
} from './output.js';

function writeJsonError(error, stream = process.stdout) {
  stream.write(`${JSON.stringify({
    ok: false,
    error: { code: error?.code ?? 'UNEXPECTED_ERROR', message: error?.message ?? String(error) },
  }, null, 2)}\n`);
}

function writeUsageError(error, argv) {
  if (argv.includes('--json')) {
    writeJsonError(error);
    return;
  }
  const command = argv[0];
  const usage = command && command !== 'help' && Object.hasOwn(COMMANDS, command)
    ? commandHelp(command, VERSION)
    : globalHelp(VERSION);
  process.stderr.write(`dotdeployer: ${error.message}\n\n${usage}\n`);
}

function writeRuntimeError(error, json) {
  if (json) {
    writeJsonError(error);
    return;
  }
  process.stderr.write(`dotdeployer: ${error?.code ?? 'UNEXPECTED_ERROR'}: ${error?.message ?? String(error)}\n`);
}

function labelRepositoryReports(report, repository) {
  if (!repository) return;
  report.projectRoot = repository;
  if (report.preflight) report.preflight.projectRoot = repository;
  if (report.localBuild?.preflight) report.localBuild.preflight.projectRoot = repository;
}

async function runProjectCommand(options) {
  let exitCode = 0;
  await withProjectSource({
    projectRoot: path.resolve(options.projectRoot),
    repository: options.repository,
    ref: options.ref,
  }, async (projectRoot) => {
    if (options.command === 'verify') {
      const report = await verifyProject({
        projectRoot,
        provider: options.provider,
        plan: options.plan,
        strict: options.strict,
        timeoutSeconds: options.timeoutSeconds,
      });
      labelRepositoryReports(report, options.repository);
      printVerificationReport(report, { json: options.json });
      if (!report.ok) exitCode = 1;
      return;
    }

    if (options.command === 'deploy') {
      const report = await deployProject({
        projectRoot,
        provider: options.provider,
        plan: options.plan,
        strict: options.strict,
        install: options.install,
        timeoutSeconds: options.timeoutSeconds,
        environment: options.environment,
        execute: options.execute,
        confirmed: options.yes,
      });
      printDeployReport(report, { json: options.json });
      if (!report.ok) exitCode = 1;
      return;
    }

    if (options.command === 'emulate') {
      const onOutput = options.json ? undefined : (stream, text) => {
        (stream === 'stderr' ? process.stderr : process.stdout).write(text);
      };
      const report = await emulateProject({
        projectRoot,
        provider: options.provider,
        plan: options.plan,
        install: options.install,
        timeoutSeconds: options.timeoutSeconds,
        strict: options.strict,
        onOutput,
      });
      labelRepositoryReports(report, options.repository);
      printEmulationReport(report, { json: options.json });
      if (!report.ok) exitCode = 1;
      return;
    }

    if (options.command === 'audit') {
      const report = await auditProject({
        projectRoot,
        provider: options.provider,
        plan: options.plan,
        strict: options.strict,
      });
      labelRepositoryReports(report, options.repository);
      if (options.sarif) process.stdout.write(`${JSON.stringify(reportToSarif(report), null, 2)}\n`);
      else printAuditReport(report, { json: options.json });
      if (!report.ok) exitCode = 1;
      return;
    }

    if (options.build) {
      const onOutput = options.json ? undefined : (stream, text) => {
        (stream === 'stderr' ? process.stderr : process.stdout).write(text);
      };
      const report = await runLocalBuild({
        projectRoot,
        provider: options.provider,
        plan: options.plan,
        install: options.install,
        timeoutSeconds: options.timeoutSeconds,
        strict: options.strict,
        onOutput,
      });
      labelRepositoryReports(report, options.repository);
      printBuildReport(report, { json: options.json });
      if (!report.preflight.ok || !report.success) exitCode = 1;
      return;
    }

    const report = await checkProject({
      projectRoot,
      provider: options.provider,
      plan: options.plan,
      strict: options.strict,
    });
    labelRepositoryReports(report, options.repository);
    if (options.sarif) process.stdout.write(`${JSON.stringify(reportToSarif(report), null, 2)}\n`);
    else printCheckReport(report, { json: options.json });
    if (!report.ok) exitCode = 1;
  });
  return exitCode;
}

export async function main(argv = process.argv.slice(2)) {
  let options;
  try {
    options = parseArgs(argv);
  } catch (error) {
    writeUsageError(error, argv);
    return 2;
  }

  if (options.help) {
    const topic = options.helpTopic ?? (options.command !== 'help' ? options.command : undefined);
    const help = topic ? commandHelp(topic, VERSION) : globalHelp(VERSION);
    process.stdout.write(`${help}\n`);
    return 0;
  }
  if (options.version) {
    process.stdout.write(`${VERSION}\n`);
    return 0;
  }
  if (options.command === 'providers') {
    printProviders(options.json);
    return 0;
  }
  if (options.command === 'mcp') {
    try {
      const { startMcpServer } = await import('../mcp/server.js');
      await startMcpServer();
      return 0;
    } catch (error) {
      writeRuntimeError(error, options.json);
      return 2;
    }
  }

  try {
    return await runProjectCommand(options);
  } catch (error) {
    writeRuntimeError(error, options.json);
    return 2;
  }
}

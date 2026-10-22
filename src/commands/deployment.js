import { mkdir, mkdtemp, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { auditProject } from './audit.js';
import { copyDeployContext, installProjectDependencies, runProcess } from './build.js';
import { VERSION } from '../core/constants.js';
import { issue, summarizeIssues } from '../core/errors.js';
import { scanProjectFiles } from '../project/files.js';
import { parseConfigFile } from '../project/config.js';
import { scanGeneratedOutput } from '../core/output.js';

const ACCESS_PROBES = Object.freeze({
  render: { cli: 'render', args: ['whoami'] },
  railway: { cli: 'railway', args: ['whoami'] },
  vercel: { cli: 'vercel', args: ['whoami'] },
  cloudflare: { cli: 'wrangler', args: ['whoami'] },
  gas: { cli: 'clasp', args: ['login', '--status'] },
});

const WRANGLER_CONFIG_NAMES = new Set(['wrangler.toml', 'wrangler.json', 'wrangler.jsonc']);
const MAX_TIMEOUT_SECONDS = 1800;

function timeoutInMs(timeoutSeconds) {
  const value = Number(timeoutSeconds);
  return Math.min(Math.max(Number.isFinite(value) ? value : 600, 1), MAX_TIMEOUT_SECONDS) * 1000;
}

function commandSummary(name, result) {
  return {
    name,
    exitCode: result.exitCode,
    signal: result.signal,
    timedOut: result.timedOut,
    durationMs: result.durationMs,
  };
}

function commandFailure(command, result, timeoutMs) {
  if (result.timedOut) {
    return issue('error', 'PROVIDER_CHECK_TIMEOUT', `${command} exceeded ${timeoutMs / 1000} seconds.`);
  }
  if (result.exitCode === null) {
    return issue('error', 'PROVIDER_CLI_UNAVAILABLE', `Could not start ${command.split(' ')[0]}; verify that the official CLI is installed and available on PATH.`);
  }
  return issue('error', 'PROVIDER_ACCESS_CHECK_FAILED', `${command} exited with status ${result.exitCode ?? result.signal ?? 'unknown'}. Check the CLI login and selected account.`);
}

/**
 * Run the local static/security gate and a read-only provider CLI identity check.
 * CLI output is intentionally discarded: whoami commands can include account
 * details, and deployment verification must never copy them into a report.
 */
export async function verifyProject({
  projectRoot = process.cwd(),
  provider = 'auto',
  plan = 'free',
  strict = false,
  timeoutSeconds = 60,
  env = process.env,
} = {}) {
  const root = path.resolve(projectRoot);
  const audit = await auditProject({ projectRoot: root, provider, plan, strict });
  const selected = Array.isArray(audit.provider) ? undefined : audit.provider;
  const issues = [...audit.issues];
  const commands = [];
  let authenticated = false;
  let authenticationChecked = false;
  const probe = Object.hasOwn(ACCESS_PROBES, selected) ? ACCESS_PROBES[selected] : undefined;
  const timeoutMs = timeoutInMs(timeoutSeconds);

  if (!probe) {
    issues.push(issue('error', 'VERIFY_PROVIDER_REQUIRED', 'Live verification requires exactly one supported provider.', undefined,
      'Choose render, railway, vercel, cloudflare, or gas.'));
  } else {
    const versionResult = await runProcess(probe.cli, ['--version'], {
      cwd: root,
      timeoutMs: Math.min(timeoutMs, 15_000),
      env,
    });
    commands.push(commandSummary(`${probe.cli} --version`, versionResult));
    if (versionResult.exitCode !== 0 || versionResult.timedOut) {
      issues.push(commandFailure(`${probe.cli} --version`, versionResult, Math.min(timeoutMs, 15_000)));
    } else {
      authenticationChecked = true;
      const accessResult = await runProcess(probe.cli, probe.args, { cwd: root, timeoutMs, env });
      commands.push(commandSummary(`${probe.cli} ${probe.args.join(' ')}`, accessResult));
      if (accessResult.exitCode === 0 && !accessResult.timedOut) {
        authenticated = true;
      } else {
        issues.push(commandFailure(`${probe.cli} ${probe.args.join(' ')}`, accessResult, timeoutMs));
      }
    }
  }

  const summary = summarizeIssues(issues);
  return {
    version: VERSION,
    projectRoot: root,
    provider: selected ?? audit.provider,
    mode: 'live-verification',
    preflight: audit.preflight,
    audit: {
      ...audit.audit,
      environment: audit.environment,
    },
    authentication: {
      checked: authenticationChecked,
      authenticated,
      identityOutputSuppressed: true,
    },
    commands,
    issues,
    summary,
    ok: audit.ok && authenticated && summary.errors === 0,
  };
}

function isProductionEnvironment(...candidates) {
  return candidates
    .filter((value) => typeof value === 'string')
    .some((value) => /(?:^|[-_.])(?:prod|production)(?:$|[-_.])/i.test(value.trim()));
}

async function getWranglerTarget(projectRoot, files, environment) {
  const configs = files.filter((file) => WRANGLER_CONFIG_NAMES.has(path.posix.basename(file.path)));
  if (configs.length !== 1) {
    return {
      error: issue('error', configs.length ? 'MULTIPLE_PROVIDER_CONFIGS' : 'WRANGLER_CONFIG_REQUIRED',
        'A single Wrangler configuration is required for the deployment workflow.'),
    };
  }
  const config = configs[0];
  const configIssues = [];
  const extension = path.extname(config.path).toLowerCase();
  const data = await parseConfigFile(projectRoot, config.path, configIssues, extension === '.jsonc' ? '.jsonc' : undefined);
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    return { error: issue('error', 'WRANGLER_CONFIG_INVALID', 'Could not read the Wrangler configuration for deployment.') };
  }
  if (data.pages_build_output_dir !== undefined || data.pagesBuildOutputDir !== undefined) {
    return {
      error: issue('error', 'PAGES_DEPLOY_UNSUPPORTED', 'The experimental deploy path currently supports Workers only; Cloudflare Pages publishing is not enabled.'),
    };
  }
  if (!data.env || typeof data.env !== 'object' || Array.isArray(data.env) || !Object.hasOwn(data.env, environment)) {
    return {
      error: issue('error', 'WRANGLER_ENVIRONMENT_NOT_CONFIGURED', `Wrangler environment ${JSON.stringify(environment)} is not declared in the selected configuration.`, config.path,
        'Declare an explicit [env.<name>] / env.<name> configuration and use a non-production environment for this experimental deploy path.'),
    };
  }
  const environmentConfig = data.env[environment];
  if (!environmentConfig || typeof environmentConfig !== 'object' || Array.isArray(environmentConfig)) {
    return { error: issue('error', 'WRANGLER_ENVIRONMENT_INVALID', `Wrangler environment ${JSON.stringify(environment)} must be an object.`, config.path) };
  }
  const workerName = typeof environmentConfig.name === 'string'
    ? environmentConfig.name
    : typeof data.name === 'string'
      ? `${data.name}-${environment}`
      : undefined;
  return {
    config,
    configDirectory: path.posix.dirname(config.path) === '.' ? '.' : path.posix.dirname(config.path),
    workerName,
  };
}

function filesUnderDirectory(files, directory) {
  if (directory === '.') return files;
  const prefix = `${directory}/`;
  return files
    .filter((file) => file.path.startsWith(prefix))
    .map((file) => ({ ...file, path: file.path.slice(prefix.length) }));
}

function deploymentError(report, code, message, file = undefined, remediation = undefined) {
  report.issues.push(issue('error', code, message, file, remediation));
  report.ok = false;
  return report;
}

/**
 * Simulate a Cloudflare Workers deployment by running local preflight, audit,
 * auth verification, and Wrangler's upload-free dry-run. A real upload is only
 * possible for an explicitly configured non-production environment and only
 * after the caller separately confirms the operation.
 */
export async function deployProject({
  projectRoot = process.cwd(),
  provider = 'cloudflare',
  plan = 'free',
  strict = false,
  install = false,
  timeoutSeconds = 600,
  environment,
  execute = false,
  confirmed = false,
  env = process.env,
} = {}) {
  const root = path.resolve(projectRoot);
  const report = {
    version: VERSION,
    projectRoot: root,
    provider,
    environment,
    mode: execute ? 'execute' : 'simulation',
    deployed: false,
    ok: false,
    verification: undefined,
    environmentDryRunPassed: false,
    commands: [],
    issues: [],
  };
  const timeoutMs = timeoutInMs(timeoutSeconds);

  if (provider !== 'cloudflare') {
    return deploymentError(report, 'DEPLOY_PROVIDER_UNSUPPORTED', 'Experimental real deployment is currently limited to Cloudflare Workers. Use verify for live access checks on the other supported providers.');
  }
  if (typeof environment !== 'string' || !environment.trim() || environment.trim() !== environment) {
    return deploymentError(report, 'DEPLOY_ENVIRONMENT_REQUIRED', 'Specify the exact named Wrangler environment with --environment.');
  }
  if (execute && !confirmed) {
    return deploymentError(report, 'DEPLOY_CONFIRMATION_REQUIRED', 'Real deployment was blocked. Add --yes together with --execute to explicitly authorize the remote deployment.');
  }
  if (execute && isProductionEnvironment(environment)) {
    return deploymentError(report, 'PRODUCTION_DEPLOY_BLOCKED', 'The experimental deployment path refuses production environments. Deploy production through the provider workflow you normally review and operate.');
  }

  const verification = await verifyProject({ projectRoot: root, provider, plan, strict, timeoutSeconds, env });
  report.verification = verification;
  if (!verification.ok) {
    return deploymentError(report, 'DEPLOY_BLOCKED_BY_VERIFICATION', 'Deployment workflow stopped because preflight, security audit, or provider authentication verification failed.');
  }
  if (execute) {
    const environmentFile = verification.issues.find((item) => item.code === 'ENV_FILE_IN_DEPLOY_CONTEXT');
    if (environmentFile) {
      return deploymentError(report, 'DEPLOY_ENV_FILE_BLOCKED', 'Real deployment is blocked because an .env file is part of the deployable context.', environmentFile.file,
        'Remove the environment file from Git/deploy context and store runtime secrets with the provider.');
    }
  }

  let files;
  try {
    files = await scanProjectFiles(root);
  } catch (error) {
    return deploymentError(report, error.code ?? 'FILE_SCAN_FAILED', error.message, error.details?.path);
  }
  const target = await getWranglerTarget(root, files, environment);
  if (target.error) {
    report.issues.push(target.error);
    return report;
  }
  if (execute && isProductionEnvironment(environment, target.workerName)) {
    return deploymentError(report, 'PRODUCTION_DEPLOY_BLOCKED', 'The selected Wrangler environment is production; the experimental deployment path only allows non-production targets.', target.config.path);
  }

  const tempParent = await mkdtemp(path.join(os.tmpdir(), 'dotdeployer-deploy-'));
  const workdir = path.join(tempParent, 'source');
  try {
    await mkdir(workdir, { recursive: true });
    await copyDeployContext(root, workdir, files);
    const cwd = path.resolve(workdir, target.configDirectory === '.' ? '' : target.configDirectory);
    if (install) {
      const localFiles = filesUnderDirectory(files, target.configDirectory);
      const step = await installProjectDependencies({ projectRoot: cwd, files: localFiles, timeoutMs, env });
      if (!step.skipped) {
        report.commands.push(commandSummary(step.name, step));
        if (step.timedOut || step.exitCode !== 0) {
          return deploymentError(report, step.timedOut ? 'INSTALL_TIMEOUT' : 'INSTALL_FAILED',
            step.timedOut ? `Dependency installation exceeded ${timeoutMs / 1000} seconds.` : `Dependency installation exited with status ${step.exitCode ?? step.signal ?? 'unknown'}.`,
            path.posix.join(target.configDirectory === '.' ? '' : target.configDirectory, 'package.json'),
            'Inspect the isolated install logs and verify the lockfile and package-manager version.');
        }
      }
    }

    const outputDirectory = path.join(tempParent, 'wrangler-output');
    const dryRunName = `wrangler deploy --dry-run --env ${environment}`;
    const dryRun = await runProcess('wrangler', [
      'deploy', '--dry-run', '--env', environment, '--outdir', outputDirectory,
    ], { cwd, timeoutMs, env });
    report.commands.push(commandSummary(dryRunName, dryRun));
    if (dryRun.timedOut || dryRun.exitCode !== 0) {
      return deploymentError(report, dryRun.timedOut ? 'DEPLOY_DRY_RUN_TIMEOUT' : 'DEPLOY_DRY_RUN_FAILED',
        dryRun.timedOut ? `Wrangler environment dry-run exceeded ${timeoutMs / 1000} seconds.` : `Wrangler environment dry-run exited with status ${dryRun.exitCode ?? dryRun.signal ?? 'unknown'}.`,
        target.config.path, 'No remote deployment was started. Inspect the Wrangler dry-run locally and correct the selected environment configuration.');
    }
    try {
      await scanGeneratedOutput(outputDirectory);
    } catch (error) {
      return deploymentError(report, error.code ?? 'DEPLOY_OUTPUT_CHECK_FAILED', error.message, target.config.path,
        'No remote deployment was started; correct the generated artifact and rerun the workflow.');
    }
    report.environmentDryRunPassed = true;

    const commandName = `wrangler deploy --env ${environment}`;
    if (!execute) {
      report.commands.push({ name: commandName, planned: true });
      report.ok = true;
      return report;
    }

    const result = await runProcess('wrangler', ['deploy', '--env', environment], { cwd, timeoutMs, env });
    report.commands.push(commandSummary(commandName, result));
    if (result.timedOut || result.exitCode !== 0) {
      return deploymentError(report, result.timedOut ? 'DEPLOY_TIMEOUT' : 'DEPLOY_FAILED',
        result.timedOut ? `Cloudflare deployment exceeded ${timeoutMs / 1000} seconds.` : `Cloudflare deployment exited with status ${result.exitCode ?? result.signal ?? 'unknown'}.`,
        target.config.path, 'Review the provider dashboard and Wrangler output locally; credentials and raw CLI output are intentionally not copied into the report.');
    }
    report.deployed = true;
    report.ok = true;
    return report;
  } catch (error) {
    return deploymentError(report, error.code ?? 'DEPLOY_RUNNER_FAILED', error.message);
  } finally {
    await rm(tempParent, { recursive: true, force: true });
  }
}

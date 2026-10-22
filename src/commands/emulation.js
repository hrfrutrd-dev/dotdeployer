import { lstat, mkdir, mkdtemp, readFile, rm, stat, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { checkProject } from './check.js';
import { VERSION } from '../core/constants.js';
import { copyDeployContext, installProjectDependencies, runLocalBuild, runProcess } from './build.js';
import { parseConfigFile } from '../project/config.js';
import { issue } from '../core/errors.js';
import { scanProjectFiles } from '../project/files.js';
import { scanGeneratedOutput } from '../core/output.js';

const CONFIG_NAMES = {
  render: new Set(['render.yaml', 'render.yml']),
  railway: new Set(['railway.json', 'railway.toml']),
  vercel: new Set(['vercel.json']),
  cloudflare: new Set(['wrangler.toml', 'wrangler.json', 'wrangler.jsonc']),
};

function addCommand(report, name, result) {
  report.commands.push({
    name,
    exitCode: result.exitCode,
    signal: result.signal,
    timedOut: result.timedOut,
    durationMs: result.durationMs,
    stdout: result.stdout,
    stderr: result.stderr,
  });
}

function configFor(files, provider) {
  const names = CONFIG_NAMES[provider];
  if (!names) return [];
  return files.filter((file) => names.has(path.posix.basename(file.path)));
}

async function parseProviderConfig(root, configFile) {
  const issues = [];
  const format = path.extname(configFile.path).toLowerCase();
  const data = await parseConfigFile(root, configFile.path, issues, format === '.jsonc' ? '.jsonc' : undefined);
  return { data, issues };
}

function fail(report, code, message, remediation = undefined) {
  report.issues.push(issue('error', code, message, undefined, remediation));
  report.success = false;
  report.ok = false;
  return report;
}

async function runStep(report, command, args, { cwd, timeoutMs, onOutput, env = process.env } = {}) {
  const display = [command, ...args].join(' ');
  const result = await runProcess(command, args, { cwd, timeoutMs, onOutput, env });
  addCommand(report, display, result);
  if (result.exitCode !== 0 || result.timedOut) {
    const missing = result.exitCode === null && /ENOENT|not found|cannot find/i.test(result.stderr);
    return {
      ok: false,
      code: missing ? 'OFFICIAL_CLI_MISSING' : result.timedOut ? 'OFFICIAL_CHECK_TIMEOUT' : 'OFFICIAL_CHECK_FAILED',
      message: missing
        ? `Required local tool "${command}" is not installed or not on PATH.`
        : result.timedOut
          ? `${display} exceeded ${timeoutMs / 1000} seconds.`
          : `${display} exited with status ${result.exitCode ?? result.signal ?? 'unknown'}.`,
    };
  }
  return { ok: true, result };
}

async function getCliVersion(report, cli, cwd, timeoutMs, onOutput, env = process.env) {
  const result = await runProcess(cli, ['--version'], { cwd, timeoutMs: Math.min(timeoutMs, 15_000), onOutput, env });
  if (result.exitCode !== 0 || result.timedOut) {
    const missing = result.exitCode === null && !result.timedOut;
    return {
      ok: false,
      code: result.timedOut ? 'OFFICIAL_CLI_TIMEOUT' : missing ? 'OFFICIAL_CLI_MISSING' : 'OFFICIAL_CLI_VERSION_FAILED',
      message: result.timedOut
        ? `${cli} --version exceeded ${Math.min(timeoutMs, 15_000) / 1000} seconds.`
        : missing
          ? `Required local tool "${cli}" is not installed or not on PATH.`
          : `${cli} --version exited with status ${result.exitCode ?? result.signal ?? 'unknown'}.`,
    };
  }
  const version = `${result.stdout}\n${result.stderr}`.trim().split(/\r?\n/).filter(Boolean).join(' ');
  report.runner = { name: cli, version: version || 'unknown' };
  return { ok: true, version };
}

function containedPath(root, candidate) {
  const relative = path.relative(path.resolve(root), path.resolve(candidate));
  return relative === '' || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative));
}

async function copyVercelProjectLink(originalRoot, workdir) {
  const linkFile = path.join(originalRoot, '.vercel', 'project.json');
  let parsed;
  try {
    const info = await lstat(linkFile);
    if (!info.isFile() || info.isSymbolicLink() || info.size >= 20_000_000) {
      return { code: 'VERCEL_PROJECT_LINK_INVALID', message: '.vercel/project.json must be a regular file smaller than 20 MB.' };
    }
    const text = await readFile(linkFile, 'utf8');
    parsed = JSON.parse(text);
  } catch (error) {
    return {
      code: 'VERCEL_PROJECT_NOT_LINKED',
      message: `Vercel CLI needs a local .vercel/project.json link. ${error.code === 'ENOENT' ? 'Run vercel link yourself first.' : `The file could not be read: ${error.message}`}`,
      remediation: 'dotdeployer never runs vercel link or pulls environment variables automatically.',
    };
  }
  if (!parsed || typeof parsed !== 'object' || typeof parsed.projectId !== 'string' || typeof parsed.orgId !== 'string') {
    return { code: 'VERCEL_PROJECT_LINK_INVALID', message: '.vercel/project.json must contain string projectId and orgId fields.' };
  }
  const target = path.join(workdir, '.vercel', 'project.json');
  await mkdir(path.dirname(target), { recursive: true });
  await writeFile(target, JSON.stringify({ projectId: parsed.projectId, orgId: parsed.orgId }, null, 2));
  return undefined;
}

function isDockerfile(files, relative = 'Dockerfile') {
  return files.some((file) => file.path === relative);
}

async function runDockerBuild(report, workdir, files, timeoutMs, onOutput, {
  dockerfile = 'Dockerfile',
  context = '.',
  source = undefined,
  env = process.env,
} = {}) {
  if (!isDockerfile(files, dockerfile)) {
    return { ok: false, code: 'DOCKERFILE_NOT_FOUND', message: `Dockerfile is not in the deployable project context: ${dockerfile}.` };
  }
  const dockerfileAbs = path.resolve(workdir, dockerfile);
  const contextAbs = path.resolve(workdir, context);
  if (!containedPath(workdir, dockerfileAbs) || !containedPath(workdir, contextAbs)) {
    return { ok: false, code: 'DOCKER_BUILD_PATH_ESCAPES_PROJECT', message: 'Dockerfile or Docker context resolves outside the temporary project.' };
  }
  const image = `dotdeployer-local-${randomUUID().slice(0, 10)}`;
  const args = ['build', '--tag', image, '--file', dockerfile, context];
  const result = await runStep(report, 'docker', args, { cwd: workdir, timeoutMs, onOutput, env });
  if (!result.ok) return result;
  const cleanup = await runProcess('docker', ['image', 'rm', image], { cwd: workdir, timeoutMs: 30_000, onOutput, env });
  if (cleanup.exitCode !== 0) {
    report.issues.push(issue('warning', 'DOCKER_IMAGE_CLEANUP_FAILED', `Could not remove temporary image ${image}.`, source));
  }
  return { ok: true };
}

async function runRender(root, workdir, files, report, timeoutMs, onOutput, install, env) {
  const configs = configFor(files, 'render');
  if (configs.length !== 1) {
    return { ok: false, code: configs.length ? 'MULTIPLE_PROVIDER_CONFIGS' : 'RENDER_BLUEPRINT_REQUIRED', message: 'Official Render validation requires exactly one render.yaml or render.yml in the selected project.' };
  }
  const { data } = await parseProviderConfig(root, configs[0]);
  const services = Array.isArray(data?.services) ? data.services.filter(Boolean) : [];
  if (services.length > 1) {
    return { ok: false, code: 'RENDER_MULTI_SERVICE_UNSUPPORTED', message: 'This version validates a Render Blueprint but cannot build multiple services together. Select or create a single-service Blueprint before emulating.' };
  }
  if (services.some((service) => service.type === 'cron')) {
    return { ok: false, code: 'RENDER_CRON_RUNTIME_UNSUPPORTED', message: 'Render cron services cannot be started as a hosted runtime by this local emulator.' };
  }
  const version = await getCliVersion(report, 'render', workdir, timeoutMs, onOutput, env);
  if (!version.ok) return version;
  const validate = await runStep(report, 'render', ['blueprints', 'validate', configs[0].path], { cwd: workdir, timeoutMs, onOutput, env });
  if (!validate.ok) return validate;

  const build = await runLocalBuild({ projectRoot: root, provider: 'render', plan: report.plan, install, timeoutSeconds: timeoutMs / 1000, strict: report.preflight.strict, onOutput });
  report.localBuild = build;
  if (!build.success) {
    return {
      ok: false,
      code: build.issues.find((item) => item.severity === 'error')?.code ?? 'RENDER_LOCAL_BUILD_FAILED',
      message: build.issues.find((item) => item.severity === 'error')?.message ?? 'Render build command did not complete successfully.',
    };
  }
  report.emulationLevel = 'official-blueprint-validation-plus-local-build';
  return { ok: true };
}

async function runVercel(root, workdir, files, report, timeoutMs, onOutput, env) {
  if (configFor(files, 'vercel').length > 1) {
    return { ok: false, code: 'MULTIPLE_PROVIDER_CONFIGS', message: 'Select one Vercel project directory before running the official build.' };
  }
  const linkError = await copyVercelProjectLink(root, workdir);
  if (linkError) return { ok: false, ...linkError };
  const version = await getCliVersion(report, 'vercel', workdir, timeoutMs, onOutput, env);
  if (!version.ok) return version;
  const build = await runStep(report, 'vercel', ['build'], { cwd: workdir, timeoutMs, onOutput, env });
  if (!build.ok) return build;
  const outputDirectory = path.join(workdir, '.vercel', 'output');
  try {
    const info = await stat(outputDirectory);
    if (!info.isDirectory()) return { ok: false, code: 'VERCEL_OUTPUT_INVALID', message: '.vercel/output is not a directory after vercel build.' };
    await scanGeneratedOutput(outputDirectory);
  } catch (error) {
    return { ok: false, code: error.code ?? 'VERCEL_OUTPUT_MISSING', message: `vercel build output validation failed: ${error.message}` };
  }
  report.emulationLevel = 'official-vercel-build-output-api';
  return { ok: true };
}

async function runCloudflare(root, workdir, tempParent, files, report, timeoutMs, onOutput, install, env) {
  const configs = configFor(files, 'cloudflare');
  if (configs.length !== 1) {
    return { ok: false, code: configs.length ? 'MULTIPLE_PROVIDER_CONFIGS' : 'WRANGLER_CONFIG_REQUIRED', message: 'Official Wrangler validation requires exactly one Wrangler config in the selected project.' };
  }
  const { data } = await parseProviderConfig(root, configs[0]);
  if (!data) return { ok: false, code: 'WRANGLER_CONFIG_INVALID', message: 'Could not read the Wrangler configuration.' };
  const configDirectory = path.posix.dirname(configs[0].path);
  const cwd = path.resolve(workdir, configDirectory === '.' ? '' : configDirectory);
  const localFiles = configDirectory === '.' ? files : files
    .filter((file) => file.path.startsWith(`${configDirectory}/`))
    .map((file) => ({ ...file, path: file.path.slice(configDirectory.length + 1) }));
  const isPages = data.pages_build_output_dir !== undefined || data.pagesBuildOutputDir !== undefined;

  // Pages is identified by its output setting, not by the absence of main.
  // Assets-only Workers have no main and still require Wrangler's dry-run.
  if (!isPages) {
    const version = await getCliVersion(report, 'wrangler', cwd, timeoutMs, onOutput, env);
    if (!version.ok) return version;

    if (install) {
      const step = await installProjectDependencies({ projectRoot: cwd, files: localFiles, timeoutMs, onOutput, env });
      if (step.skipped) {
        report.issues.push(issue('warning', 'INSTALL_SKIPPED', 'No root package.json was found; Wrangler dependency installation was skipped.'));
      } else {
        addCommand(report, step.name, step);
        if (step.timedOut || step.exitCode !== 0) {
          return {
            ok: false,
            code: step.timedOut ? 'INSTALL_TIMEOUT' : 'INSTALL_FAILED',
            message: step.timedOut ? `Dependency installation exceeded ${timeoutMs / 1000} seconds.` : `Dependency installation exited with status ${step.exitCode ?? step.signal ?? 'unknown'}.`,
          };
        }
      }
    }

    const outdir = path.join(tempParent, 'wrangler-output');
    const result = await runStep(report, 'wrangler', ['deploy', '--dry-run', '--outdir', outdir], { cwd, timeoutMs, onOutput, env });
    if (!result.ok) return result;
    try {
      if (!(await stat(outdir)).isDirectory()) return { ok: false, code: 'WRANGLER_OUTPUT_INVALID', message: 'Wrangler dry-run output path is not a directory.' };
      await scanGeneratedOutput(outdir);
    } catch (error) {
      return { ok: false, code: error.code ?? 'WRANGLER_OUTPUT_MISSING', message: `Wrangler dry-run output validation failed: ${error.message}` };
    }
    report.emulationLevel = 'official-wrangler-dry-run';
    return { ok: true };
  }

  // Wrangler has no upload-free Pages deploy command. Run the configured local
  // build only; never call `wrangler pages deploy` from a preflight tool.
  const build = await runLocalBuild({ projectRoot: root, provider: 'cloudflare', plan: report.plan, install, timeoutSeconds: timeoutMs / 1000, strict: report.preflight.strict, onOutput });
  report.localBuild = build;
  if (!build.success) {
    return {
      ok: false,
      code: build.issues.find((item) => item.severity === 'error')?.code ?? 'PAGES_LOCAL_BUILD_FAILED',
      message: build.issues.find((item) => item.severity === 'error')?.message ?? 'Cloudflare Pages local build did not complete successfully.',
    };
  }
  report.emulationLevel = 'local-pages-build-only';
  report.issues.push(issue('warning', 'PAGES_RUNTIME_NOT_EMULATED', 'Pages Functions, edge bindings, and the hosted Pages runtime were not started; only the configured local build was tested.'));
  return { ok: true };
}

async function runRailway(root, workdir, files, report, timeoutMs, onOutput, env) {
  const configs = configFor(files, 'railway');
  if (configs.length > 1) {
    return { ok: false, code: 'MULTIPLE_PROVIDER_CONFIGS', message: 'Select one Railway project directory before emulating it.' };
  }
  let data = {};
  if (configs.length === 1) {
    const parsed = await parseProviderConfig(root, configs[0]);
    if (!parsed.data) return { ok: false, code: 'RAILWAY_CONFIG_INVALID', message: 'Could not read the Railway configuration.' };
    data = parsed.data;
  }
  const build = data.build ?? {};
  const hasDockerfile = files.some((file) => path.posix.basename(file.path).toLowerCase() === 'dockerfile');
  const builder = String(build.builder ?? (hasDockerfile ? 'DOCKERFILE' : 'RAILPACK')).toUpperCase();

  if (builder === 'DOCKERFILE') {
    const configDir = configs.length ? path.posix.dirname(configs[0].path) : '.';
    const dockerfileValue = typeof build.dockerfilePath === 'string' ? build.dockerfilePath : 'Dockerfile';
    const contextValue = typeof build.dockerContext === 'string' ? build.dockerContext : '.';
    const dockerfile = path.posix.normalize(path.posix.join(configDir === '.' ? '' : configDir, dockerfileValue));
    const context = path.posix.normalize(path.posix.join(configDir === '.' ? '' : configDir, contextValue));
    const result = await runDockerBuild(report, workdir, files, timeoutMs, onOutput, { dockerfile, context, source: configs[0]?.path, env });
    if (!result.ok) return result;
    report.emulationLevel = 'local-docker-build';
    return { ok: true };
  }

  if (builder !== 'RAILPACK') {
    return { ok: false, code: 'RAILWAY_BUILDER_UNSUPPORTED', message: `No local runner is registered for Railway builder ${builder}.` };
  }
  const version = await getCliVersion(report, 'railpack', workdir, timeoutMs, onOutput, env);
  if (!version.ok) return version;
  const pinned = build.railpackVersion;
  if (pinned && !version.version.includes(String(pinned))) {
    return { ok: false, code: 'RAILPACK_VERSION_MISMATCH', message: `railway.json requests Railpack ${pinned}, but local ${version.version} does not match. Use the same Railpack release before treating this result as comparable.` };
  }
  const image = `dotdeployer-railway-${randomUUID().slice(0, 10)}`;
  const args = ['build', '--name', image, '--progress', 'plain'];
  if (typeof build.buildCommand === 'string' && build.buildCommand.trim()) args.push('--build-cmd', build.buildCommand);
  if (typeof data.deploy?.startCommand === 'string' && data.deploy.startCommand.trim()) args.push('--start-cmd', data.deploy.startCommand);
  args.push(workdir);
  const result = await runStep(report, 'railpack', args, { cwd: workdir, timeoutMs, onOutput, env });
  if (!result.ok) return result;
  const cleanup = await runProcess('docker', ['image', 'rm', image], { cwd: workdir, timeoutMs: 30_000, onOutput, env });
  if (cleanup.exitCode !== 0) report.issues.push(issue('warning', 'DOCKER_IMAGE_CLEANUP_FAILED', `Could not remove temporary image ${image}.`));
  report.emulationLevel = 'local-railpack-image-build';
  return { ok: true };
}

/**
 * Run a provider's own local compiler/validator where one exists. This is
 * intentionally not called an exact platform emulator: managed services and
 * remote quotas remain outside the local process.
 */
export async function emulateProject({
  projectRoot = process.cwd(),
  provider = 'auto',
  plan = 'free',
  strict = false,
  install = false,
  timeoutSeconds = 600,
  onOutput = undefined,
  env = process.env,
} = {}) {
  const root = path.resolve(projectRoot);
  const preflight = await checkProject({ projectRoot: root, provider, plan, strict });
  const selected = Array.isArray(preflight.provider) ? undefined : preflight.provider;
  const report = {
    version: VERSION,
    projectRoot: root,
    provider: selected ?? preflight.provider,
    plan,
    preflight,
    emulationLevel: 'none',
    limitations: [
      'Hosted runtimes, managed services, bindings, secrets, production networking, quotas, and provider-side state are not fully reproduced.',
      'dotdeployer does not invoke deployment or publish commands; official CLIs may still use network access, provider APIs, or registry credentials for validation and builds.',
      'Only known generated output directories are size-scanned; Docker image layers are not. Multi-service Render Blueprints fail closed rather than being partially built.'
    ],
    runner: undefined,
    commands: [],
    localBuild: undefined,
    issues: [],
    success: false,
    ok: false,
  };

  if (!preflight.ok) return fail(report, 'BUILD_BLOCKED_BY_PREFLIGHT', 'Emulation was not started because preflight has errors.');
  if (!selected || ['auto', 'all', 'generic'].includes(selected)) {
    return fail(report, 'EMULATOR_PROVIDER_REQUIRED', 'Choose one explicit provider for official local emulation.', 'Use --provider render, railway, vercel, cloudflare, or gas.');
  }
  if (selected === 'gas') {
    const local = await runLocalBuild({ projectRoot: root, provider: 'gas', plan, install, timeoutSeconds, strict });
    report.localBuild = local;
    return fail(report, 'GAS_LOCAL_RUNTIME_UNAVAILABLE', 'Google Apps Script does not provide an official local runtime emulator. Only manifest validation and JavaScript syntax checks can run here; executing Apps Script APIs requires Google-hosted execution.',
      'Use a separate Apps Script test project and an authenticated Apps Script API execution workflow for integration tests.');
  }

  let files;
  try {
    files = await scanProjectFiles(root);
  } catch (error) {
    return fail(report, error.code ?? 'FILE_SCAN_FAILED', error.message);
  }
  const timeoutMs = Math.min(Math.max(Number(timeoutSeconds) || 600, 1), 1800) * 1000;
  const tempParent = await mkdtemp(path.join(os.tmpdir(), 'dotdeployer-emulate-'));
  const workdir = path.join(tempParent, 'source');
  try {
    await mkdir(workdir, { recursive: true });
    await copyDeployContext(root, workdir, files);
    if (install && ['railway', 'vercel'].includes(selected)) {
      report.issues.push(issue('info', 'NATIVE_INSTALL_MANAGED', `The ${selected} build tool manages dependency installation; no separate package-manager install step was added.`));
    }

    let result;
    if (selected === 'render') result = await runRender(root, workdir, files, report, timeoutMs, onOutput, install, env);
    else if (selected === 'railway') result = await runRailway(root, workdir, files, report, timeoutMs, onOutput, env);
    else if (selected === 'vercel') result = await runVercel(root, workdir, files, report, timeoutMs, onOutput, env);
    else if (selected === 'cloudflare') result = await runCloudflare(root, workdir, tempParent, files, report, timeoutMs, onOutput, install, env);
    else result = { ok: false, code: 'EMULATOR_PROVIDER_UNSUPPORTED', message: `No official local runner is registered for ${selected}.` };

    if (!result.ok) {
      report.issues.push(issue('error', result.code, result.message, result.file, result.remediation));
      return report;
    }
    report.success = report.issues.every((item) => item.severity !== 'error');
    report.ok = report.success;
    return report;
  } catch (error) {
    report.issues.push(issue('error', error.code ?? 'EMULATOR_FAILED', error.message));
    return report;
  } finally {
    await rm(tempParent, { recursive: true, force: true });
  }
}

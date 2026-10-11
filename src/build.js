import { spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { cp, lstat, mkdir, mkdtemp, readFile, readlink, rm, stat, symlink } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import { checkProject } from './check.js';
import { DotdeployerError, issue } from './errors.js';
import { scanProjectFiles } from './files.js';
import { parseConfigFile } from './config.js';
import { scanGeneratedOutput } from './output.js';

const MAX_LOG_CHARS = 250_000;

function escapesRoot(root, candidate) {
  const relative = path.relative(path.resolve(root), path.resolve(candidate));
  return relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative);
}

function captureTail(current, chunk) {
  const text = current + chunk.toString('utf8');
  return text.length > MAX_LOG_CHARS ? `[earlier output truncated]\n${text.slice(-MAX_LOG_CHARS)}` : text;
}

export function runProcess(command, args, {
  cwd,
  timeoutMs = 600_000,
  shell = false,
  env = process.env,
  onOutput = undefined,
} = {}) {
  return new Promise((resolve) => {
    const start = Date.now();
    let stdout = '';
    let stderr = '';
    let timedOut = false;
    let settled = false;
    let killTimer;
    let child;

    try {
      child = spawn(command, args, {
        cwd,
        env,
        shell,
        stdio: ['ignore', 'pipe', 'pipe'],
        detached: process.platform !== 'win32',
        windowsHide: true,
      });
    } catch (error) {
      resolve({ exitCode: null, signal: null, stdout, stderr: error.message, timedOut, durationMs: Date.now() - start });
      return;
    }

    const append = (stream, chunk) => {
      const text = chunk.toString('utf8');
      if (stream === 'stdout') stdout = captureTail(stdout, chunk);
      else stderr = captureTail(stderr, chunk);
      onOutput?.(stream, text);
    };
    child.stdout.on('data', (chunk) => append('stdout', chunk));
    child.stderr.on('data', (chunk) => append('stderr', chunk));
    child.on('error', (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({ exitCode: null, signal: null, stdout, stderr: `${stderr}${error.message}`, timedOut, durationMs: Date.now() - start });
    });
    child.on('close', (code, signal) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      clearTimeout(killTimer);
      resolve({ exitCode: code, signal, stdout, stderr, timedOut, durationMs: Date.now() - start });
    });

    const timer = setTimeout(() => {
      timedOut = true;
      if (!child.pid) return;
      try {
        if (process.platform !== 'win32') process.kill(-child.pid, 'SIGTERM');
        else child.kill('SIGTERM');
      } catch {}
      killTimer = setTimeout(() => {
        try {
          if (process.platform !== 'win32') process.kill(-child.pid, 'SIGKILL');
          else child.kill('SIGKILL');
        } catch {}
      }, 2_000);
      killTimer.unref?.();
    }, timeoutMs);
    timer.unref?.();
  });
}

export async function copyDeployContext(projectRoot, destination, files) {
  for (const file of files) {
    const target = path.resolve(destination, file.path);
    if (escapesRoot(destination, target)) {
      throw new DotdeployerError(`Project file escapes copy directory: ${file.path}`, { code: 'PATH_ESCAPE' });
    }
    await mkdir(path.dirname(target), { recursive: true });
    const info = await lstat(file.absolute);
    if (info.isSymbolicLink()) {
      const sourceTarget = path.resolve(path.dirname(file.absolute), await readlink(file.absolute));
      const projectRelativeTarget = path.relative(path.resolve(projectRoot), sourceTarget);
      if (projectRelativeTarget === '..' || projectRelativeTarget.startsWith(`..${path.sep}`) || path.isAbsolute(projectRelativeTarget)) {
        throw new DotdeployerError(`Symbolic link escapes the project while copying: ${file.path}`, { code: 'EXTERNAL_SYMLINK' });
      }
      const copiedTarget = path.resolve(destination, projectRelativeTarget);
      const copiedLink = path.relative(path.dirname(target), copiedTarget) || '.';
      await symlink(copiedLink, target);
    } else {
      await cp(file.absolute, target, { recursive: true, dereference: false, preserveTimestamps: true, force: true });
    }
  }
}

function packageManagerFrom(packageJson, files) {
  const locks = [];
  if (files.some((file) => file.path === 'package-lock.json' || file.path === 'npm-shrinkwrap.json')) locks.push('npm');
  if (files.some((file) => file.path === 'pnpm-lock.yaml')) locks.push('pnpm');
  if (files.some((file) => file.path === 'yarn.lock')) locks.push('yarn');
  if (files.some((file) => file.path === 'bun.lock' || file.path === 'bun.lockb')) locks.push('bun');

  const declared = packageJson?.packageManager;
  if (declared) {
    const manager = declared.split('@')[0].toLowerCase();
    if (!['npm', 'pnpm', 'yarn', 'bun'].includes(manager)) {
      throw new DotdeployerError(`Unsupported package manager ${declared}.`, { code: 'PACKAGE_MANAGER_UNSUPPORTED' });
    }
    if (!locks.includes(manager)) {
      throw new DotdeployerError(`package.json declares ${manager}, but its lockfile is missing.`, { code: 'PACKAGE_MANAGER_LOCK_MISSING' });
    }
    return { manager, declared };
  }
  if (locks.length !== 1) {
    throw new DotdeployerError(
      locks.length === 0
        ? 'Cannot install dependencies reproducibly: no recognized lockfile was found.'
        : `Cannot choose an installer because multiple lockfiles were found: ${locks.join(', ')}.`,
      { code: locks.length === 0 ? 'LOCKFILE_REQUIRED_FOR_INSTALL' : 'PACKAGE_MANAGER_AMBIGUOUS' },
    );
  }
  return { manager: locks[0], declared: undefined };
}

function packageManagerForScript(packageJson, files) {
  const declared = packageJson?.packageManager?.split('@')[0]?.toLowerCase();
  if (['npm', 'pnpm', 'yarn', 'bun'].includes(declared)) return declared;
  const found = [];
  if (files.some((file) => file.path === 'package-lock.json' || file.path === 'npm-shrinkwrap.json')) found.push('npm');
  if (files.some((file) => file.path === 'pnpm-lock.yaml')) found.push('pnpm');
  if (files.some((file) => file.path === 'yarn.lock')) found.push('yarn');
  if (files.some((file) => file.path === 'bun.lock' || file.path === 'bun.lockb')) found.push('bun');
  return found.length === 1 ? found[0] : 'npm';
}

function installerCommand(manager, declared) {
  switch (manager) {
    case 'npm': return { command: 'npm', args: ['ci', '--no-audit', '--no-fund'] };
    case 'pnpm': return { command: 'pnpm', args: ['install', '--frozen-lockfile'] };
    case 'yarn': {
      const version = /^yarn@(\d+)/.exec(declared ?? '')?.[1];
      return { command: 'yarn', args: ['install', ...(version === '1' || !version ? ['--frozen-lockfile'] : ['--immutable'])] };
    }
    case 'bun': return { command: 'bun', args: ['install', '--frozen-lockfile'] };
    default: throw new DotdeployerError(`Unsupported package manager ${manager}.`, { code: 'PACKAGE_MANAGER_UNSUPPORTED' });
  }
}

export async function installProjectDependencies({
  projectRoot,
  files,
  timeoutMs = 600_000,
  onOutput = undefined,
  env = process.env,
} = {}) {
  const packageFile = files.find((file) => file.path === 'package.json');
  if (!packageFile) return { skipped: true, name: undefined };
  const packageJson = JSON.parse(await readFile(path.join(projectRoot, 'package.json'), 'utf8'));
  const selected = packageManagerFrom(packageJson, files);
  const installer = installerCommand(selected.manager, selected.declared);
  const name = `${installer.command} ${installer.args.join(' ')}`;
  return {
    skipped: false,
    name,
    ...await runProcess(installer.command, installer.args, { cwd: projectRoot, timeoutMs, onOutput, env }),
  };
}

function scriptRunCommand(manager, scriptName) {
  if (manager === 'pnpm') return { command: 'pnpm', args: ['run', scriptName] };
  if (manager === 'yarn') return { command: 'yarn', args: [scriptName] };
  if (manager === 'bun') return { command: 'bun', args: ['run', scriptName] };
  return { command: 'npm', args: ['run', scriptName] };
}

async function readProviderBuildCommand(projectRoot, provider, files) {
  const namesByProvider = {
    render: new Set(['render.yaml', 'render.yml']),
    railway: new Set(['railway.json', 'railway.toml']),
    vercel: new Set(['vercel.json']),
    cloudflare: new Set(['wrangler.toml', 'wrangler.json', 'wrangler.jsonc']),
  };
  const names = namesByProvider[provider];
  if (!names) return undefined;
  const configs = files.filter((file) => names.has(path.posix.basename(file.path)));
  for (const config of configs) {
    const issues = [];
    const jsonc = path.extname(config.path) === '.jsonc';
    const data = await parseConfigFile(projectRoot, config.path, issues, jsonc ? '.jsonc' : undefined);
    if (!data || typeof data !== 'object' || Array.isArray(data)) continue;
    const configDir = path.posix.dirname(config.path) === '.' ? '.' : path.posix.dirname(config.path);

    if (provider === 'render') {
      const service = Array.isArray(data.services)
        ? data.services.find((entry) => entry && entry.type !== 'cron' && (
          (typeof entry.buildCommand === 'string' && entry.buildCommand.trim())
          || String(entry.runtime ?? '').toLowerCase() === 'docker'
          || typeof entry.dockerfilePath === 'string'
        ))
        : undefined;
      if (service) {
        const cwd = service.rootDir ? path.posix.normalize(path.posix.join(configDir === '.' ? '' : configDir, service.rootDir)) : configDir;
        if (String(service.runtime ?? '').toLowerCase() === 'docker' || service.dockerfilePath) {
          const dockerfile = typeof service.dockerfilePath === 'string' ? service.dockerfilePath : 'Dockerfile';
          const dockerContext = typeof service.dockerContext === 'string' ? service.dockerContext : '.';
          return {
            kind: 'docker',
            dockerfilePath: path.posix.normalize(path.posix.join(cwd === '.' ? '' : cwd, dockerfile)),
            dockerContext: path.posix.normalize(path.posix.join(cwd === '.' ? '' : cwd, dockerContext)),
            source: config.path,
          };
        }
        return {
          command: service.buildCommand,
          cwd,
          source: config.path,
          outputDirectory: service.type === 'static' && typeof service.staticPublishPath === 'string' ? service.staticPublishPath : undefined,
        };
      }
    } else if (provider === 'railway' && typeof data.build?.buildCommand === 'string' && data.build.buildCommand.trim()) {
      return { command: data.build.buildCommand, cwd: configDir, source: config.path };
    } else if (provider === 'railway' && (String(data.build?.builder ?? '').toUpperCase() === 'DOCKERFILE' || data.build?.dockerfilePath)) {
      const dockerfile = typeof data.build.dockerfilePath === 'string' ? data.build.dockerfilePath : 'Dockerfile';
      const dockerContext = typeof data.build.dockerContext === 'string' ? data.build.dockerContext : '.';
      return {
        kind: 'docker',
        dockerfilePath: path.posix.normalize(path.posix.join(configDir === '.' ? '' : configDir, dockerfile)),
        dockerContext: path.posix.normalize(path.posix.join(configDir === '.' ? '' : configDir, dockerContext)),
        source: config.path,
      };
    } else if (provider === 'vercel' && typeof data.buildCommand === 'string' && data.buildCommand.trim()) {
      return { command: data.buildCommand, cwd: configDir, source: config.path };
    } else if (provider === 'cloudflare' && typeof data.build?.command === 'string' && data.build.command.trim()) {
      const outputDirectory = typeof data.pages_build_output_dir === 'string'
        ? data.pages_build_output_dir
        : typeof data.assets?.directory === 'string' ? data.assets.directory : undefined;
      return { command: data.build.command, cwd: configDir, source: config.path, outputDirectory };
    }
  }
  return undefined;
}

async function readConfiguredOutputDirectory(projectRoot, provider, files) {
  const configNames = {
    render: new Set(['render.yaml', 'render.yml']),
    vercel: new Set(['vercel.json']),
    cloudflare: new Set(['wrangler.toml', 'wrangler.json', 'wrangler.jsonc']),
  }[provider];
  if (!configNames) return undefined;
  const configs = files.filter((file) => configNames.has(path.posix.basename(file.path)));
  if (configs.length !== 1) return undefined;
  const config = configs[0];
  const errors = [];
  const data = await parseConfigFile(projectRoot, config.path, errors, path.extname(config.path) === '.jsonc' ? '.jsonc' : undefined);
  if (!data || typeof data !== 'object' || Array.isArray(data)) return undefined;
  const configDir = path.posix.dirname(config.path) === '.' ? '.' : path.posix.dirname(config.path);
  if (provider === 'vercel' && typeof data.outputDirectory === 'string') {
    return { cwd: configDir, directory: data.outputDirectory };
  }
  if (provider === 'cloudflare') {
    const directory = typeof data.pages_build_output_dir === 'string'
      ? data.pages_build_output_dir
      : typeof data.assets?.directory === 'string' ? data.assets.directory : undefined;
    if (directory) return { cwd: configDir, directory };
  }
  if (provider === 'render' && Array.isArray(data.services)) {
    const service = data.services.find((entry) => entry?.type === 'static' && typeof entry.staticPublishPath === 'string');
    if (service) {
      const cwd = service.rootDir ? path.posix.normalize(path.posix.join(configDir === '.' ? '' : configDir, service.rootDir)) : configDir;
      return { cwd, directory: service.staticPublishPath };
    }
  }
  return undefined;
}

async function validateBuiltOutput(projectRoot, buildPlan, issues) {
  if (!buildPlan?.outputDirectory) return;
  const output = path.resolve(projectRoot, buildPlan.cwd === '.' ? '' : buildPlan.cwd, buildPlan.outputDirectory);
  if (escapesRoot(projectRoot, output)) {
    issues.push(issue('error', 'BUILD_OUTPUT_ESCAPES_PROJECT', 'Configured output directory resolves outside the temporary build project.', buildPlan.source));
    return;
  }
  try {
    const info = await stat(output);
    if (!info.isDirectory()) {
      issues.push(issue('error', 'BUILD_OUTPUT_NOT_DIRECTORY', `Build output is not a directory: ${buildPlan.outputDirectory}`, buildPlan.source));
    } else {
      await scanGeneratedOutput(output);
    }
  } catch (error) {
    if (error?.code === 'ENOENT') issues.push(issue('error', 'BUILD_OUTPUT_MISSING', `Build succeeded but expected output directory was not created: ${buildPlan.outputDirectory}`, buildPlan.source,
      'Check the configured output directory against the build script and provider settings.'));
    else issues.push(issue('error', error.code ?? 'BUILD_OUTPUT_CHECK_FAILED', error.message, error.details?.path ?? buildPlan.source));
  }
}

async function getBuildPlan(projectRoot, provider, files, packageJson) {
  const scripts = packageJson?.scripts ?? {};
  if (provider === 'gas' && !(typeof scripts.build === 'string' && scripts.build.trim())) {
    const sources = files.filter((file) => file.path.toLowerCase().endsWith('.gs'));
    return { kind: 'gas-syntax', sources, cwd: '.' };
  }
  const providerCommand = await readProviderBuildCommand(projectRoot, provider, files);
  if (providerCommand?.kind === 'docker') return providerCommand;
  if (providerCommand) {
    let outputDirectory = providerCommand.outputDirectory;
    if (provider === 'vercel') {
      const errors = [];
      const data = await parseConfigFile(projectRoot, providerCommand.source, errors);
      if (typeof data?.outputDirectory === 'string') outputDirectory = data.outputDirectory;
    }
    return { ...providerCommand, outputDirectory, kind: 'shell' };
  }
  if (typeof scripts.build === 'string' && scripts.build.trim()) {
    const manager = packageManagerForScript(packageJson, files);
    const run = scriptRunCommand(manager, 'build');
    const output = await readConfiguredOutputDirectory(projectRoot, provider, files);
    return {
      command: [run.command, ...run.args].join(' '),
      cwd: '.',
      source: 'package.json#scripts.build',
      outputDirectory: output ? path.posix.join(output.cwd === '.' ? '' : output.cwd, output.directory) : undefined,
      kind: 'shell',
    };
  }
  if (files.some((file) => path.posix.basename(file.path).toLowerCase() === 'dockerfile')) {
    return { kind: 'docker', cwd: '.' };
  }
  return undefined;
}

function checkGasSources(files, issues) {
  const sources = files.filter((file) => file.path.toLowerCase().endsWith('.gs'));
  if (!sources.length) {
    issues.push(issue('warning', 'GAS_SOURCE_NOT_FOUND', 'No .gs source files were found; only the manifest can be checked.'));
    return;
  }
  for (const source of sources) {
    try {
      new vm.Script(readFileSync(source.absolute, 'utf8'), { filename: source.path });
      issues.push(issue('info', 'GAS_SYNTAX_OK', `JavaScript syntax parsed: ${source.path}`, source.path));
    } catch (error) {
      issues.push(issue('error', 'GAS_SYNTAX_ERROR', `Syntax error in ${source.path}: ${error.message}`, source.path));
    }
  }
}

/** Run opt-in local build commands in a temporary copy of the Git deploy context. */
export async function runLocalBuild({
  projectRoot = process.cwd(),
  provider = 'auto',
  plan = 'free',
  install = false,
  timeoutSeconds = 600,
  strict = false,
  onOutput = undefined,
} = {}) {
  const root = path.resolve(projectRoot);
  const preflight = await checkProject({ projectRoot: root, provider, plan, strict });
  const report = { preflight, attempted: false, success: false, workDirectory: undefined, commands: [], issues: [] };
  if (!preflight.ok) {
    report.issues.push(issue('error', 'BUILD_BLOCKED_BY_PREFLIGHT', 'Build was not started because preflight has errors.'));
    return report;
  }

  if (Array.isArray(preflight.provider)) {
    report.issues.push(issue('error', 'BUILD_PROVIDER_AMBIGUOUS', `Multiple provider configs were selected (${preflight.provider.join(', ')}); choose one provider for a local build.`));
    return report;
  }
  const providerId = preflight.provider;
  const effectiveProvider = providerId === 'auto' || providerId === 'all' ? 'generic' : providerId;
  let files;
  try {
    files = await scanProjectFiles(root);
  } catch (error) {
    report.issues.push(issue('error', error.code ?? 'FILE_SCAN_FAILED', error.message, error.details?.path));
    return report;
  }

  const configNames = {
    render: new Set(['render.yaml', 'render.yml']),
    railway: new Set(['railway.json', 'railway.toml']),
    vercel: new Set(['vercel.json']),
    cloudflare: new Set(['wrangler.toml', 'wrangler.json', 'wrangler.jsonc']),
  }[effectiveProvider];
  if (configNames && files.filter((file) => configNames.has(path.posix.basename(file.path))).length > 1) {
    report.issues.push(issue('error', 'BUILD_PROVIDER_CONFIG_AMBIGUOUS', `Found multiple ${effectiveProvider} config files. Narrow the project path before building.`));
    return report;
  }

  const tempParent = await mkdtemp(path.join(os.tmpdir(), 'dotdeployer-build-'));
  const workdir = path.join(tempParent, 'source');
  report.workDirectory = '<temporary isolated copy>';
  const timeoutMs = Math.min(Math.max(Number(timeoutSeconds) || 600, 1), 1800) * 1000;
  try {
    await mkdir(workdir, { recursive: true });
    await copyDeployContext(root, workdir, files);
    const packageFile = files.find((file) => file.path === 'package.json');
    const packageJson = packageFile ? JSON.parse(await readFile(packageFile.absolute, 'utf8')) : undefined;
    const buildPlan = await getBuildPlan(root, effectiveProvider, files, packageJson);

    if (install) {
      const step = await installProjectDependencies({ projectRoot: workdir, files, timeoutMs, onOutput });
      if (step.skipped) {
        report.issues.push(issue('warning', 'INSTALL_SKIPPED', 'No root package.json was found; dependency installation was skipped.'));
      } else {
        report.attempted = true;
        report.commands.push({
          name: step.name,
          cwd: '.', exitCode: step.exitCode, signal: step.signal, timedOut: step.timedOut,
          durationMs: step.durationMs, stdout: step.stdout, stderr: step.stderr,
        });
        if (step.timedOut || step.exitCode !== 0) {
          report.issues.push(issue('error', step.timedOut ? 'INSTALL_TIMEOUT' : 'INSTALL_FAILED',
            step.timedOut ? `Dependency installation exceeded ${timeoutMs / 1000} seconds.` : `Dependency installation exited with status ${step.exitCode ?? step.signal ?? 'unknown'}.`,
            'package.json', 'Inspect install logs and verify the lockfile and package-manager version.'));
          return report;
        }
      }
    }

    if (buildPlan?.kind === 'gas-syntax') {
      report.attempted = true;
      checkGasSources(files, report.issues);
    } else if (buildPlan?.kind === 'docker') {
      const dockerfile = buildPlan.dockerfilePath
        ?? files.find((file) => path.posix.basename(file.path).toLowerCase() === 'dockerfile')?.path;
      if (!dockerfile || !files.some((file) => file.path === dockerfile)) {
        report.issues.push(issue('error', 'DOCKERFILE_NOT_FOUND', `Dockerfile is not part of the deployable project context: ${dockerfile ?? '(not configured)'}.`, buildPlan.source));
        return report;
      }
      const context = buildPlan.dockerContext ?? '.';
      const dockerfileAbs = path.resolve(workdir, dockerfile);
      const contextAbs = path.resolve(workdir, context);
      for (const candidate of [dockerfileAbs, contextAbs]) {
        if (escapesRoot(workdir, candidate)) {
          report.issues.push(issue('error', 'DOCKER_BUILD_PATH_ESCAPES_PROJECT', 'Dockerfile or build context resolves outside the temporary project.', buildPlan.source));
          return report;
        }
      }
      const image = `dotdeployer-preflight:${randomUUID().slice(0, 12)}`;
      const args = ['build', '--tag', image, '--file', dockerfile, context];
      report.attempted = true;
      const step = await runProcess('docker', args, { cwd: workdir, timeoutMs, onOutput });
      report.commands.push({
        name: `docker ${args.join(' ')}`, cwd: '.', exitCode: step.exitCode, signal: step.signal,
        timedOut: step.timedOut, durationMs: step.durationMs, stdout: step.stdout, stderr: step.stderr,
      });
      if (step.exitCode === 0) await runProcess('docker', ['image', 'rm', image], { cwd: workdir, timeoutMs: 30_000 });
      if (step.timedOut || step.exitCode !== 0) {
        report.issues.push(issue('error', step.timedOut ? 'DOCKER_BUILD_TIMEOUT' : 'DOCKER_BUILD_FAILED',
          step.timedOut ? `Docker build exceeded ${timeoutMs / 1000} seconds.` : `Docker build exited with status ${step.exitCode ?? step.signal ?? 'unknown'}.`,
          dockerfile, 'Check the Docker build logs and confirm Docker is installed and running.'));
        return report;
      }
    } else if (buildPlan?.kind === 'shell') {
      const cwd = path.resolve(workdir, buildPlan.cwd === '.' ? '' : buildPlan.cwd);
      if (escapesRoot(workdir, cwd)) {
        report.issues.push(issue('error', 'BUILD_CWD_ESCAPES_PROJECT', 'Configured build root resolves outside the temporary project.', buildPlan.source));
        return report;
      }
      report.attempted = true;
      const step = await runProcess(buildPlan.command, [], { cwd, timeoutMs, shell: true, onOutput });
      report.commands.push({
        name: buildPlan.command, cwd: buildPlan.cwd, source: buildPlan.source,
        exitCode: step.exitCode, signal: step.signal, timedOut: step.timedOut,
        durationMs: step.durationMs, stdout: step.stdout, stderr: step.stderr,
      });
      if (step.timedOut || step.exitCode !== 0) {
        report.issues.push(issue('error', step.timedOut ? 'BUILD_TIMEOUT' : 'BUILD_FAILED',
          step.timedOut ? `Build exceeded ${timeoutMs / 1000} seconds.` : `Build exited with status ${step.exitCode ?? step.signal ?? 'unknown'}.`,
          buildPlan.source, 'Inspect the captured build logs; this command ran in a temporary copy of the project.'));
        return report;
      }
      await validateBuiltOutput(workdir, buildPlan, report.issues);
    } else {
      report.issues.push(issue('error', 'BUILD_COMMAND_NOT_FOUND', 'No provider build command, package.json build script, Dockerfile, or Apps Script source files were found.', undefined,
        'Set a provider build command or add a package.json build script.'));
      return report;
    }

    report.success = report.issues.every((item) => item.severity !== 'error');
    return report;
  } catch (error) {
    report.issues.push(issue('error', error.code ?? 'BUILD_RUNNER_FAILED', error.message));
    return report;
  } finally {
    await rm(tempParent, { recursive: true, force: true });
    report.workDirectory = undefined;
  }
}

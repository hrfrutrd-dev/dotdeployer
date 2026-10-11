import path from 'node:path';
import { MAX_FILE_SIZE_BYTES, VERSION } from '../core/constants.js';
import { DotdeployerError, issue, summarizeIssues } from '../core/errors.js';
import { scanProjectFiles } from '../project/files.js';
import { detectProviders, normalizeProvider, validateGenericProject, validateProvider } from '../providers/index.js';

export async function checkProject({
  projectRoot = process.cwd(),
  provider = 'auto',
  plan = 'free',
  strict = false,
} = {}) {
  const root = path.resolve(projectRoot);
  const issues = [];
  let files;

  // Keep this as the first validation stage. In particular, no project config
  // is parsed and no subprocess/build is started until the size guard passes.
  try {
    files = await scanProjectFiles(root);
  } catch (error) {
    const details = error instanceof DotdeployerError ? error.details : undefined;
    issues.push(issue('error', error.code ?? 'FILE_SCAN_FAILED', error.message, details?.path,
      error.code === 'FILE_TOO_LARGE'
        ? 'Remove the oversized file from the Git deployment context (and Git history if it is already committed), then rerun the check.'
        : undefined));
    const counts = summarizeIssues(issues);
    return {
      version: VERSION,
      projectRoot: root,
      provider: provider ?? 'auto',
      plan,
      ok: false,
      filesScanned: details?.filesScanned ?? 0,
      maxFileSizeBytes: MAX_FILE_SIZE_BYTES,
      summary: counts,
      issues,
      stoppedBeforeConfigValidation: true,
    };
  }

  const chosen = normalizeProvider(provider);
  if (!chosen) {
    issues.push(issue('error', 'PROVIDER_UNKNOWN', `Unknown provider: ${String(provider)}.`, undefined,
      'Choose render, railway, vercel, cloudflare, gas, auto, or all.'));
    const counts = summarizeIssues(issues);
    return {
      version: VERSION, projectRoot: root, provider, plan, ok: false,
      filesScanned: files.length, maxFileSizeBytes: MAX_FILE_SIZE_BYTES,
      summary: counts, issues,
    };
  }

  const detected = detectProviders(files);
  let selectedProviders;
  if (chosen === 'auto') selectedProviders = detected.length ? detected : [];
  else if (chosen === 'all') selectedProviders = detected;
  else if (chosen === 'generic') selectedProviders = [];
  else selectedProviders = [chosen];

  await validateGenericProject(root, files, issues);

  if (selectedProviders.length === 0) {
    if (chosen === 'auto' || chosen === 'all') {
      issues.push(issue('info', 'NO_PROVIDER_CONFIG_DETECTED', 'No supported provider config was detected; only generic project checks were run.', undefined,
        'Specify --provider to validate a project that relies on provider auto-detection.'));
    }
  } else {
    for (const selected of selectedProviders) {
      await validateProvider(root, files, selected, plan, issues);
    }
  }

  if (strict) {
    for (const item of issues) {
      if (item.severity === 'warning') item.severity = 'error';
    }
  }

  const summary = summarizeIssues(issues);
  return {
    version: VERSION,
    projectRoot: root,
    provider: selectedProviders.length > 1 ? selectedProviders : selectedProviders[0] ?? (chosen === 'generic' ? 'generic' : chosen),
    detectedProviders: detected,
    plan,
    ok: summary.errors === 0,
    filesScanned: files.length,
    maxFileSizeBytes: MAX_FILE_SIZE_BYTES,
    strict,
    summary,
    issues,
  };
}

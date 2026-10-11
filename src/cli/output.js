import { VERSION, PROVIDERS, MAX_FILE_SIZE_BYTES } from '../core/constants.js';
import { providerInfo } from '../providers/index.js';

function providerLabel(provider) {
  if (Array.isArray(provider)) return provider.map((id) => PROVIDERS[id]?.label ?? id).join(', ');
  return PROVIDERS[provider]?.label ?? (provider === 'auto' ? 'auto-detect' : provider);
}

function issuePrefix(severity) {
  if (severity === 'error') return '✖';
  if (severity === 'warning') return '⚠';
  return 'ℹ';
}

export function printCheckReport(report, { json = false } = {}) {
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

export function printAuditReport(report, { json = false } = {}) {
  if (json) {
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
    return;
  }
  printCheckReport(report.preflight);
  process.stdout.write(`\nDeployment security audit: ${report.ok ? 'PASS' : 'FAIL'} — ${report.summary.errors} error(s), ${report.summary.warnings} warning(s), ${report.summary.infos} info\n`);
  process.stdout.write(`Deploy-context files: ${report.audit.filesInContext}; text scanned: ${report.audit.scannedTextFiles}; binary skipped: ${report.audit.skippedBinaryFiles}\n`);
  process.stdout.write(`Environment variables: ${report.environment.referenced} referenced, ${report.environment.documented} documented, ${report.environment.configured} configured\n`);
  for (const item of report.auditIssues) {
    const location = item.file ? ` ${item.file}${item.line ? `:${item.line}` : ''}:` : '';
    process.stdout.write(`${issuePrefix(item.severity)} [${item.code}]${location} ${item.message}\n`);
    if (item.remediation) process.stdout.write(`  Fix: ${item.remediation}\n`);
  }
  process.stdout.write('\nSecret values are never printed. This is a high-confidence static scan of deployable text files, not proof that every credential or runtime environment issue has been found.\n');
}

export function printBuildReport(report, { json = false } = {}) {
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

export function printEmulationReport(report, { json = false } = {}) {
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

export function printVerificationReport(report, { json = false } = {}) {
  if (json) {
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
    return;
  }
  printCheckReport(report.preflight);
  process.stdout.write(`\nLive verification: ${report.ok ? 'PASS' : 'FAIL'}\n`);
  process.stdout.write(`Provider CLI authentication: ${report.authentication.authenticated ? 'VERIFIED' : 'NOT VERIFIED'} (identity output suppressed)\n`);
  process.stdout.write(`Deployment-context text files scanned: ${report.audit.scannedTextFiles ?? 0}; credential findings: ${report.audit.secretFindings ?? 0}\n`);
  for (const command of report.commands) {
    const status = command.timedOut ? 'TIMEOUT' : command.exitCode === 0 ? 'PASS' : `EXIT ${command.exitCode ?? command.signal ?? 'unknown'}`;
    process.stdout.write(`  ${status} (${(command.durationMs / 1000).toFixed(1)}s): ${command.name}\n`);
  }
  const preflightIssues = new Set(report.preflight.issues);
  for (const item of report.issues) {
    if (preflightIssues.has(item)) continue;
    process.stdout.write(`${issuePrefix(item.severity)} [${item.code}]${item.file ? ` ${item.file}${item.line ? `:${item.line}` : ''}:` : ''} ${item.message}\n`);
    if (item.remediation) process.stdout.write(`  Fix: ${item.remediation}\n`);
  }
  process.stdout.write('Provider identity and raw CLI output are deliberately omitted from this report.\n');
}

export function printDeployReport(report, { json = false } = {}) {
  if (json) {
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
    return;
  }
  process.stdout.write(`dotdeployer ${VERSION} — deployment workflow\n`);
  process.stdout.write(`Project: ${report.projectRoot}\n`);
  process.stdout.write(`Target: ${providerLabel(report.provider)} / Wrangler environment ${report.environment ?? '(not set)'}\n`);
  process.stdout.write(`Mode: ${report.mode === 'execute' ? 'REAL DEPLOY' : 'SIMULATION'} — ${report.deployed ? 'DEPLOYED' : report.ok ? 'NOT DEPLOYED' : 'BLOCKED'}\n`);
  process.stdout.write(`Provider authentication: ${report.verification?.authentication?.authenticated ? 'verified' : 'not verified'}; environment dry-run: ${report.environmentDryRunPassed ? 'PASS' : 'not run'}\n`);
  if (report.verification?.issues?.length) {
    process.stdout.write('Verification findings:\n');
    for (const item of report.verification.issues) {
      process.stdout.write(`${issuePrefix(item.severity)} [${item.code}]${item.file ? ` ${item.file}:` : ''} ${item.message}\n`);
      if (item.remediation) process.stdout.write(`  Fix: ${item.remediation}\n`);
    }
  }
  for (const command of report.commands) {
    const status = command.planned ? 'PLANNED' : command.timedOut ? 'TIMEOUT' : command.exitCode === 0 ? 'PASS' : `EXIT ${command.exitCode ?? command.signal ?? 'unknown'}`;
    process.stdout.write(`  ${status}${command.durationMs !== undefined ? ` (${(command.durationMs / 1000).toFixed(1)}s)` : ''}: ${command.name}\n`);
  }
  for (const item of report.issues) {
    process.stdout.write(`${issuePrefix(item.severity)} [${item.code}]${item.file ? ` ${item.file}:` : ''} ${item.message}\n`);
    if (item.remediation) process.stdout.write(`  Fix: ${item.remediation}\n`);
  }
  process.stdout.write('Real execution is limited to confirmed, explicitly configured non-production Cloudflare Workers environments.\n');
}

export function printProviders(asJson) {
  const info = providerInfo();
  if (asJson) {
    process.stdout.write(`${JSON.stringify({ providers: info, fileSizeLimitBytes: MAX_FILE_SIZE_BYTES }, null, 2)}\n`);
    return;
  }
  process.stdout.write('Supported static adapters:\n');
  for (const provider of info) process.stdout.write(`  ${provider.id.padEnd(12)} ${provider.label} — ${provider.description}\n`);
  process.stdout.write('\nFree/paid is a policy label, not a full recreation of current vendor quotas. See docs/README.md for scope and limitations.\n');
}


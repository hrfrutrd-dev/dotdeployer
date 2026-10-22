import { open, readFile } from 'node:fs/promises';
import path from 'node:path';
import { checkProject } from './check.js';
import { parseConfigFile } from '../project/config.js';
import { VERSION } from '../core/constants.js';
import { issue, summarizeIssues } from '../core/errors.js';
import { scanProjectFiles } from '../project/files.js';

const MAX_SECRET_FINDINGS = 100;
const MAX_ENVIRONMENT_SOURCES = 20;

const ENVIRONMENT_SOURCE_EXTENSIONS = new Set([
  '.astro', '.cjs', '.cts', '.js', '.jsx', '.mjs', '.mts', '.svelte', '.ts', '.tsx', '.vue',
]);

const ENVIRONMENT_TEMPLATE_NAMES = new Set([
  '.env.example', '.env.sample', '.env.template', '.env.defaults',
]);

const PUBLIC_ENVIRONMENT_PREFIXES = [
  'NEXT_PUBLIC_', 'VITE_', 'REACT_APP_', 'NUXT_PUBLIC_', 'GATSBY_', 'EXPO_PUBLIC_',
  'VUE_APP_', 'PUBLIC_', 'ASTRO_PUBLIC_',
];

const PLATFORM_ENVIRONMENT_NAMES = new Set([
  'CI', 'DEBUG', 'HOME', 'HOST', 'HOSTNAME', 'LOGNAME', 'NODE_ENV', 'PATH', 'PORT',
  'PWD', 'SHELL', 'SHLVL', 'TERM', 'TMPDIR', 'USER', 'VERCEL', 'VERCEL_BRANCH_URL',
  'VERCEL_DEPLOYMENT_ID', 'VERCEL_ENV', 'VERCEL_REGION', 'VERCEL_URL',
]);

const SECRET_RULES = [
  {
    id: 'PRIVATE_KEY',
    severity: 'error',
    message: 'A private-key block is committed in the deployable context.',
    remediation: 'Remove the private key from Git and inject it through the provider secret manager or a protected CI secret.',
    pattern: /-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----/g,
  },
  {
    id: 'AWS_ACCESS_KEY',
    severity: 'error',
    message: 'A value matching an AWS access key ID is committed in the deployable context.',
    remediation: 'Revoke or rotate the key immediately, remove it from Git history, and use an IAM role or provider secret manager.',
    pattern: /\b(?:A3T[A-Z0-9]|AKIA|ASIA)[A-Z0-9]{16}\b/g,
  },
  {
    id: 'GITHUB_TOKEN',
    severity: 'error',
    message: 'A value matching a GitHub token is committed in the deployable context.',
    remediation: 'Revoke the token, remove it from Git history, and use a protected secret or workload identity.',
    pattern: /\b(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{60,})\b/g,
  },
  {
    id: 'GITLAB_TOKEN',
    severity: 'error',
    message: 'A value matching a GitLab personal access token is committed in the deployable context.',
    remediation: 'Revoke the token, remove it from Git history, and provide it through a protected secret.',
    pattern: /\bglpat-[A-Za-z0-9_-]{20,}\b/g,
  },
  {
    id: 'SLACK_TOKEN',
    severity: 'error',
    message: 'A value matching a Slack token is committed in the deployable context.',
    remediation: 'Revoke the token and move it to the provider secret manager.',
    pattern: /\b(?:xox(?:a|b|p|r|s)|xapp)-[A-Za-z0-9-]{10,}\b/g,
  },
  {
    id: 'STRIPE_SECRET_KEY',
    severity: 'error',
    message: 'A value matching a Stripe secret or restricted key is committed in the deployable context.',
    remediation: 'Roll the key, remove it from Git history, and inject it at deploy time.',
    pattern: /\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b/g,
  },
  {
    id: 'OPENAI_API_KEY',
    severity: 'error',
    message: 'A value matching an OpenAI project, service-account, or admin API key is committed in the deployable context.',
    remediation: 'Revoke the key, remove it from Git history, and inject it through a protected secret.',
    pattern: /\bsk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,}\b/g,
  },
  {
    id: 'GOOGLE_API_KEY',
    severity: 'error',
    message: 'A value matching a Google API key is committed in the deployable context.',
    remediation: 'Restrict or rotate the key and move it to a provider-managed secret.',
    pattern: /\bAIza[0-9A-Za-z_-]{35}\b/g,
  },
  {
    id: 'URL_CREDENTIALS',
    severity: 'error',
    message: 'A URL with an embedded username and password is committed in the deployable context.',
    remediation: 'Remove the credentials from the URL, rotate them if they are real, and provide them through a protected secret.',
    pattern: /\b[a-z][a-z0-9+.-]*:\/\/[^\s/:@]+:[^\s/@]+@/gi,
    ignore: (match) => isPlaceholderCredentialUrl(match[0]),
  },
  {
    id: 'NPM_TOKEN',
    severity: 'error',
    message: 'An npm auth token is committed in .npmrc.',
    remediation: 'Revoke the token and reference a protected environment variable instead of committing it.',
    pattern: /^\s*\/\/[^\s]+:_authToken\s*=\s*(?!\$\{?[A-Za-z_][A-Za-z0-9_]*\}?(?:\s*(?:#.*)?)?$)[^\s#]+/gm,
    files: new Set(['.npmrc']),
  },
  {
    id: 'NETRC_PASSWORD',
    severity: 'error',
    message: 'A password is committed in .netrc.',
    remediation: 'Remove .netrc from the deployable context and use a protected credential store or CI secret.',
    pattern: /\bpassword\s+[^\s#]+/g,
    files: new Set(['.netrc']),
  },
];

const SENSITIVE_FILENAME_RULES = [
  {
    pattern: /^(?:id_rsa|id_dsa|id_ecdsa|id_ed25519)$/i,
    severity: 'error',
    code: 'CREDENTIAL_FILE_IN_DEPLOY_CONTEXT',
    message: 'A private SSH key filename is part of the deployable context.',
    remediation: 'Remove private keys from Git and provide credentials through the provider secret manager.',
  },
  {
    pattern: /\.(?:p12|pfx|key)$/i,
    severity: 'warning',
    code: 'SENSITIVE_KEYSTORE_IN_DEPLOY_CONTEXT',
    message: 'A key or keystore file is part of the deployable context.',
    remediation: 'Confirm this file contains no private material; prefer a provider secret or protected artifact store.',
  },
];

function relativeBasename(filePath) {
  return path.posix.basename(filePath).toLowerCase();
}

function extension(filePath) {
  return path.posix.extname(filePath).toLowerCase();
}

function isLikelyBinary(buffer) {
  const sample = buffer.subarray(0, Math.min(buffer.length, 8_192));
  if (sample.includes(0)) return true;
  if (!sample.length) return false;
  let controlCharacters = 0;
  for (const byte of sample) {
    if (byte < 7 || (byte > 13 && byte < 32)) controlCharacters += 1;
  }
  return controlCharacters / sample.length > 0.2;
}

async function readAuditableText(file) {
  // Inspect a small prefix before loading the entire file. Static sites often
  // include large images or archives, and a security audit should not hold
  // those binary assets in memory merely to classify them.
  const handle = await open(file.absolute, 'r');
  try {
    const sample = Buffer.alloc(8_192);
    const { bytesRead } = await handle.read(sample, 0, sample.length, 0);
    if (isLikelyBinary(sample.subarray(0, bytesRead))) return { binary: true };
  } finally {
    await handle.close();
  }

  const bytes = await readFile(file.absolute);
  if (isLikelyBinary(bytes)) return { binary: true };
  return { binary: false, bytes, text: bytes.toString('utf8') };
}

function lineLookup(text) {
  const starts = [0];
  for (let index = 0; index < text.length; index += 1) {
    if (text.charCodeAt(index) === 10) starts.push(index + 1);
  }
  return (offset) => {
    let low = 0;
    let high = starts.length;
    while (low + 1 < high) {
      const middle = Math.floor((low + high) / 2);
      if (starts[middle] <= offset) low = middle;
      else high = middle;
    }
    return low + 1;
  };
}

function issueAt(severity, code, message, file, line, remediation = undefined) {
  const result = issue(severity, code, message, file, remediation);
  if (Number.isInteger(line) && line > 0) result.line = line;
  return result;
}

function addIssueWithLimit(state, item) {
  if (state.secretFindings >= MAX_SECRET_FINDINGS) {
    if (!state.truncated) {
      state.truncated = true;
      state.issues.push(issue('warning', 'SECRET_FINDINGS_TRUNCATED',
        `Secret scanning stopped after ${MAX_SECRET_FINDINGS} findings to keep the report safe and actionable.`, undefined,
        'Resolve the reported findings and rerun the audit to reveal any remaining matches.'));
    }
    return false;
  }
  state.issues.push(item);
  state.secretFindings += 1;
  return true;
}

function scanSensitiveFilename(file, state) {
  const basename = relativeBasename(file.path);
  for (const rule of SENSITIVE_FILENAME_RULES) {
    if (!rule.pattern.test(basename)) continue;
    addIssueWithLimit(state, issue(rule.severity, rule.code, rule.message, file.path, rule.remediation));
  }
}

function scanSecrets(text, file, state, lineAt) {
  const basename = relativeBasename(file.path);
  for (const rule of SECRET_RULES) {
    if (rule.files && !rule.files.has(basename)) continue;
    rule.pattern.lastIndex = 0;
    let match;
    while ((match = rule.pattern.exec(text))) {
      if (rule.ignore?.(match)) continue;
      const added = addIssueWithLimit(state, issueAt(
        rule.severity,
        `SECRET_${rule.id}`,
        rule.message,
        file.path,
        lineAt(match.index),
        rule.remediation,
      ));
      if (!added) return;
      // Avoid a pathological zero-width expression from ever spinning. All
      // shipped rules consume text, but this keeps the scanner future-proof.
      if (match[0].length === 0) rule.pattern.lastIndex += 1;
    }
  }
}

function isEnvironmentSource(filePath) {
  return ENVIRONMENT_SOURCE_EXTENSIONS.has(extension(filePath));
}

function addEnvironmentReference(references, name, source) {
  if (!references.has(name)) references.set(name, []);
  const sources = references.get(name);
  if (sources.length >= MAX_ENVIRONMENT_SOURCES) return;
  if (sources.some((item) => item.file === source.file && item.line === source.line && item.kind === source.kind)) return;
  sources.push(source);
}

function extractEnvironmentReferences(text, file, references, lineAt) {
  const patterns = [
    { kind: 'process.env', pattern: /\bprocess\s*\.\s*env\s*(?:\?\s*)?\.\s*([A-Za-z_][A-Za-z0-9_]*)/g, group: 1 },
    { kind: 'process.env', pattern: /\bprocess\s*\.\s*env\s*\[\s*(['"])([A-Za-z_][A-Za-z0-9_]*)\1\s*\]/g, group: 2 },
    { kind: 'import.meta.env', pattern: /\bimport\s*\.\s*meta\s*\.\s*env\s*(?:\?\s*)?\.\s*([A-Za-z_][A-Za-z0-9_]*)/g, group: 1 },
    { kind: 'Deno.env', pattern: /\bDeno\s*\.\s*env\s*\.\s*get\s*\(\s*(['"])([A-Za-z_][A-Za-z0-9_]*)\1/g, group: 2 },
    { kind: 'Bun.env', pattern: /\bBun\s*\.\s*env\s*(?:\?\s*)?\.\s*([A-Za-z_][A-Za-z0-9_]*)/g, group: 1 },
  ];

  for (const { kind, pattern, group } of patterns) {
    let match;
    while ((match = pattern.exec(text))) {
      addEnvironmentReference(references, match[group], {
        file: file.path,
        line: lineAt(match.index),
        kind,
      });
      if (match[0].length === 0) pattern.lastIndex += 1;
    }
  }
}

function normalizeEnvironmentValue(rawValue) {
  const text = String(rawValue ?? '').trim();
  if (!text || text.startsWith('#')) return '';
  const quote = text[0];
  if (quote === '"' || quote === "'") {
    for (let index = 1; index < text.length; index += 1) {
      if (text[index] !== quote || text[index - 1] === '\\') continue;
      const trailing = text.slice(index + 1).trim();
      if (!trailing || trailing.startsWith('#')) return text.slice(1, index).trim();
      break;
    }
    return text;
  }
  return text.replace(/\s+#.*$/, '').trim();
}

function isPlaceholderValue(rawValue) {
  const value = normalizeEnvironmentValue(rawValue);
  if (!value || /^\$(?:\{[A-Za-z_][A-Za-z0-9_]*\}|[A-Za-z_][A-Za-z0-9_]*)$/.test(value)) return true;
  if (/^<[^>]*>$/.test(value)) return true;
  return /^(?:change[-_ ]?me|replace[-_ ]?me|redacted|masked|placeholder|dummy|example|sample|test(?:ing)?|password|pass|secret|token|api[-_ ]?key|user(?:name)?|not[-_ ]?a[-_ ]?real[-_ ]?.*|your[-_ ].*|x+|\*+)$/i.test(value);
}

function isPlaceholderCredentialUrl(urlPrefix) {
  const schemeEnd = urlPrefix.indexOf('://');
  const userInfo = urlPrefix.slice(schemeEnd + 3, -1);
  const separator = userInfo.indexOf(':');
  return separator < 0 || isPlaceholderValue(userInfo.slice(separator + 1));
}

function isEnvironmentFile(filePath) {
  const basename = relativeBasename(filePath);
  return /^\.env(?:\..+)?$/i.test(basename) && !ENVIRONMENT_TEMPLATE_NAMES.has(basename);
}

function scanEnvironmentFileValues(text, file, state) {
  if (!isEnvironmentFile(file.path)) return;
  for (const [index, line] of text.split(/\r?\n/).entries()) {
    const match = /^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/.exec(line);
    if (!match || !environmentSensitivity(match[1]) || isPlaceholderValue(match[2])) continue;

    // URLs without embedded credentials are often local endpoints rather than
    // secrets. URL credentials themselves are covered by SECRET_URL_CREDENTIALS.
    const value = normalizeEnvironmentValue(match[2]);
    if (/^[a-z][a-z0-9+.-]*:\/\//i.test(value)) continue;

    const added = addIssueWithLimit(state, issueAt(
      'error',
      'SECRET_ENVIRONMENT_VALUE',
      `Sensitive environment variable ${match[1]} has a literal value in a committed environment file.`,
      file.path,
      index + 1,
      'Remove the value from Git, rotate it if real, and inject it through the provider secret manager. Keep only names or placeholders in committed templates.',
    ));
    if (!added) return;
  }
}

function collectTemplateVariables(text, file, documented) {
  const lines = text.split(/\r?\n/);
  for (const [offset, line] of lines.entries()) {
    const match = /^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=/.exec(line);
    if (!match) continue;
    const name = match[1];
    if (!documented.has(name)) documented.set(name, []);
    documented.get(name).push({ file: file.path, line: offset + 1 });
  }
}

function addConfiguredEnvironment(configured, name, file, kind) {
  if (typeof name !== 'string' || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) return;
  if (!configured.has(name)) configured.set(name, []);
  const sources = configured.get(name);
  if (!sources.some((source) => source.file === file && source.kind === kind)) sources.push({ file, kind });
}

function collectRenderEnvironment(data, file, configured) {
  const groups = Array.isArray(data.envVarGroups) ? data.envVarGroups : [];
  const services = Array.isArray(data.services) ? data.services : [];
  for (const owner of [...groups, ...services]) {
    if (!owner || typeof owner !== 'object') continue;
    for (const variable of Array.isArray(owner.envVars) ? owner.envVars : []) {
      if (variable && typeof variable === 'object') addConfiguredEnvironment(configured, variable.key, file, 'Render envVar');
    }
  }
}

async function collectConfiguredEnvironment(projectRoot, files, configured) {
  const names = new Set([
    'render.yaml', 'render.yml', 'railway.json', 'railway.toml', 'vercel.json',
    'wrangler.toml', 'wrangler.json', 'wrangler.jsonc',
  ]);
  for (const file of files) {
    const basename = path.posix.basename(file.path);
    if (!names.has(basename)) continue;
    const parseIssues = [];
    const data = await parseConfigFile(
      projectRoot,
      file.path,
      parseIssues,
      extension(file.path) === '.jsonc' ? '.jsonc' : undefined,
    );
    if (!data || typeof data !== 'object' || Array.isArray(data)) continue;

    if (basename === 'vercel.json') {
      for (const name of Object.keys(data.env && typeof data.env === 'object' ? data.env : {})) {
        addConfiguredEnvironment(configured, name, file.path, 'Vercel env');
      }
    } else if (basename.startsWith('wrangler.')) {
      for (const name of Object.keys(data.vars && typeof data.vars === 'object' ? data.vars : {})) {
        addConfiguredEnvironment(configured, name, file.path, 'Wrangler vars');
      }
    } else if (basename === 'render.yaml' || basename === 'render.yml') {
      collectRenderEnvironment(data, file.path, configured);
    } else if (basename === 'railway.json' || basename === 'railway.toml') {
      for (const name of Object.keys(data.variables && typeof data.variables === 'object' ? data.variables : {})) {
        addConfiguredEnvironment(configured, name, file.path, 'Railway variables');
      }
    }
  }
}

function isPublicEnvironmentVariable(name, sources) {
  return PUBLIC_ENVIRONMENT_PREFIXES.some((prefix) => name.startsWith(prefix))
    || sources.some((source) => source.kind === 'import.meta.env');
}

function environmentSensitivity(name) {
  if (/(?:^|_)(?:SECRET|TOKEN|PASSWORD|PASS|PRIVATE|CREDENTIAL|DATABASE_URL|REDIS_URL|AUTH|AWS|STRIPE|OPENAI)(?:_|$)/i.test(name)) return 'high';
  if (/(?:^|_)(?:API_?KEY|KEY)(?:_|$)/i.test(name)) return 'possible';
  return undefined;
}

function isPlatformProvidedEnvironment(name) {
  return PLATFORM_ENVIRONMENT_NAMES.has(name)
    || /^(?:CF_|CLOUDFLARE_|GITHUB_|RAILWAY_|RENDER_|VERCEL_)/.test(name);
}

function makeEnvironmentReport(references, documented, configured, auditIssues) {
  const variables = [...references.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([name, sources]) => {
      const isPublic = isPublicEnvironmentVariable(name, sources);
      const sensitivity = environmentSensitivity(name);
      const isDocumented = documented.has(name);
      const isConfigured = configured.has(name);
      const first = sources[0];

      if (!isDocumented && !isConfigured && !isPlatformProvidedEnvironment(name)) {
        auditIssues.push(issueAt(
          'warning',
          'ENVIRONMENT_VARIABLE_UNDOCUMENTED',
          `Environment variable ${name} is referenced in source but is not documented by an .env.example-style file or provider configuration.`,
          first.file,
          first.line,
          'Document the variable (without its secret value) in .env.example or declare its non-secret configuration at the provider.',
        ));
      }
      if (isPublic && sensitivity === 'high') {
        auditIssues.push(issueAt(
          'error',
          'SENSITIVE_ENVIRONMENT_EXPOSED_TO_CLIENT',
          `Sensitive-looking environment variable ${name} is exposed to client-side code.`,
          first.file,
          first.line,
          'Remove the public client prefix and access the value only from server-side code. Use a public identifier instead of a credential when possible.',
        ));
      } else if (isPublic && sensitivity === 'possible') {
        auditIssues.push(issueAt(
          'warning',
          'POSSIBLY_SENSITIVE_ENVIRONMENT_EXPOSED_TO_CLIENT',
          `API-key-like environment variable ${name} is exposed to client-side code.`,
          first.file,
          first.line,
          'Confirm the key is intentionally public, restricted to browser origins, and cannot grant privileged access.',
        ));
      }

      return {
        name,
        public: isPublic,
        sensitivity: sensitivity ?? 'none',
        documented: isDocumented,
        configured: isConfigured,
        sources,
        documentedIn: documented.get(name) ?? [],
        configuredIn: configured.get(name) ?? [],
      };
    });
  return variables;
}

/**
 * Scan the deployable Git context for high-confidence committed credentials and
 * build an environment-variable contract. This command never executes project
 * code, contacts a provider, or includes matched secret values in its report.
 */
export async function auditProject({
  projectRoot = process.cwd(),
  provider = 'auto',
  plan = 'free',
  strict = false,
} = {}) {
  const root = path.resolve(projectRoot);
  const preflight = await checkProject({ projectRoot: root, provider, plan, strict });
  const auditIssues = [];
  const auditState = {
    issues: auditIssues,
    secretFindings: 0,
    truncated: false,
  };
  const audit = {
    filesInContext: 0,
    scannedTextFiles: 0,
    skippedBinaryFiles: 0,
    scannedBytes: 0,
    secretFindings: 0,
    findingsTruncated: false,
  };
  const environment = {
    variables: [],
    referenced: 0,
    documented: 0,
    configured: 0,
  };

  if (!preflight.stoppedBeforeConfigValidation) {
    let files;
    try {
      files = await scanProjectFiles(root);
    } catch (error) {
      auditIssues.push(issue('error', error.code ?? 'AUDIT_FILE_SCAN_FAILED', error.message, error.details?.path));
    }

    if (files) {
      audit.filesInContext = files.length;
      const references = new Map();
      const documented = new Map();
      const configured = new Map();
      for (const file of files) {
        scanSensitiveFilename(file, auditState);
        let content;
        try {
          content = await readAuditableText(file);
        } catch {
          auditIssues.push(issue('warning', 'AUDIT_FILE_READ_FAILED', `Could not read ${file.path} while auditing it.`, file.path,
            'Check file permissions and rerun the audit.'));
          continue;
        }
        if (content.binary) {
          audit.skippedBinaryFiles += 1;
          continue;
        }
        const { bytes, text } = content;
        const lineAt = lineLookup(text);
        audit.scannedTextFiles += 1;
        audit.scannedBytes += bytes.length;
        scanSecrets(text, file, auditState, lineAt);
        scanEnvironmentFileValues(text, file, auditState);
        if (ENVIRONMENT_TEMPLATE_NAMES.has(relativeBasename(file.path))) {
          collectTemplateVariables(text, file, documented);
        }
        if (isEnvironmentSource(file.path)) {
          extractEnvironmentReferences(text, file, references, lineAt);
        }
      }
      await collectConfiguredEnvironment(root, files, configured);
      environment.variables = makeEnvironmentReport(references, documented, configured, auditIssues);
      environment.referenced = references.size;
      environment.documented = documented.size;
      environment.configured = configured.size;
    }
  }

  if (strict) {
    for (const item of auditIssues) {
      if (item.severity === 'warning') item.severity = 'error';
    }
  }

  audit.secretFindings = auditState.secretFindings;
  audit.findingsTruncated = auditState.truncated;
  const issues = [...preflight.issues, ...auditIssues];
  const summary = summarizeIssues(issues);
  return {
    version: VERSION,
    projectRoot: root,
    provider: preflight.provider,
    detectedProviders: preflight.detectedProviders,
    plan,
    strict,
    preflight,
    audit,
    environment,
    summary,
    issues,
    auditIssues,
    ok: summary.errors === 0,
  };
}

const PROJECT_COMMANDS = ['check', 'audit', 'test', 'emulate', 'verify', 'deploy'];
const REPOSITORY_COMMANDS = ['check', 'audit', 'test', 'emulate', 'verify'];
const BUILD_COMMANDS = ['check', 'test'];
const TIMEOUT_COMMANDS = ['check', 'test', 'emulate', 'verify', 'deploy'];
const REPORT_COMMANDS = [...PROJECT_COMMANDS, 'providers'];

export const COMMANDS = Object.freeze({
  check: {
    usage: 'dotdeployer check [path] [options]',
    description: 'Validate deploy-context files and provider configuration without contacting a hosting API.',
    options: ['projectRoot', 'repository', 'ref', 'provider', 'plan', 'strict', 'build', 'install', 'timeoutSeconds', 'json', 'sarif'],
    acceptsPath: true,
  },
  audit: {
    usage: 'dotdeployer audit [path] [options]',
    description: 'Audit deployable files for committed credentials and environment-variable exposure.',
    options: ['projectRoot', 'repository', 'ref', 'provider', 'plan', 'strict', 'json', 'sarif'],
    acceptsPath: true,
  },
  test: {
    usage: 'dotdeployer test [path] [options]',
    description: 'Run preflight checks and optionally build the project in a temporary copy.',
    options: ['projectRoot', 'repository', 'ref', 'provider', 'plan', 'strict', 'build', 'install', 'timeoutSeconds', 'json'],
    acceptsPath: true,
  },
  emulate: {
    usage: 'dotdeployer emulate [path] --provider <name> [options]',
    description: 'Use an official provider CLI for local validation or build where supported; never deploy.',
    options: ['projectRoot', 'repository', 'ref', 'provider', 'plan', 'strict', 'install', 'timeoutSeconds', 'json'],
    acceptsPath: true,
  },
  verify: {
    usage: 'dotdeployer verify [path] --provider <name> [options]',
    description: 'Run local security checks and verify provider CLI authentication without publishing.',
    options: ['projectRoot', 'repository', 'ref', 'provider', 'plan', 'strict', 'timeoutSeconds', 'json'],
    acceptsPath: true,
  },
  deploy: {
    usage: 'dotdeployer deploy [path] --provider cloudflare --environment <name> [options]',
    description: 'Simulate a Cloudflare Workers deployment. A real non-production deploy requires --execute --yes.',
    options: ['projectRoot', 'provider', 'plan', 'strict', 'install', 'timeoutSeconds', 'environment', 'execute', 'yes', 'json'],
    acceptsPath: true,
  },
  help: {
    usage: 'dotdeployer help [command]',
    description: 'Show global help or help for one command.',
    options: [],
    acceptsPath: false,
  },
  providers: {
    usage: 'dotdeployer providers [--json]',
    description: 'List supported provider adapters and their configuration formats.',
    options: ['json'],
    acceptsPath: false,
  },
  mcp: {
    usage: 'dotdeployer mcp',
    description: 'Start the MCP stdio server for Claude Code and other MCP clients.',
    options: [],
    acceptsPath: false,
  },
});

export const OPTION_DEFINITIONS = Object.freeze([
  {
    key: 'projectRoot',
    names: ['--path'],
    value: '<dir>',
    description: 'Local project directory (default: .).',
    commands: PROJECT_COMMANDS,
  },
  {
    key: 'repository',
    names: ['--repo'],
    value: '<owner/repo|URL>',
    description: 'Clone a GitHub repository into a temporary directory.',
    commands: REPOSITORY_COMMANDS,
  },
  {
    key: 'ref',
    names: ['--ref'],
    value: '<branch-or-tag>',
    description: 'Git ref to clone (requires --repo).',
    commands: REPOSITORY_COMMANDS,
  },
  {
    key: 'provider',
    names: ['--provider'],
    value: '<name>',
    description: 'Provider: auto, all, render, railway, vercel, cloudflare, gas, or generic.',
    commands: PROJECT_COMMANDS,
  },
  {
    key: 'plan',
    names: ['--plan'],
    value: '<name>',
    description: 'Assumed local policy profile (default: free).',
    commands: PROJECT_COMMANDS,
  },
  {
    key: 'strict',
    names: ['--strict'],
    description: 'Treat every warning as an error.',
    commands: PROJECT_COMMANDS,
  },
  {
    key: 'build',
    names: ['--build'],
    description: 'Run a build command in a temporary copy.',
    commands: BUILD_COMMANDS,
  },
  {
    key: 'install',
    names: ['--install'],
    description: 'Install from the lockfile in the temporary copy (with --build, emulate, or deploy).',
    commands: ['check', 'test', 'emulate', 'deploy'],
  },
  {
    key: 'timeoutSeconds',
    names: ['--timeout'],
    value: '<seconds>',
    description: 'Per-command timeout in seconds (1–1800; verify defaults to 60, otherwise 600).',
    commands: TIMEOUT_COMMANDS,
  },
  {
    key: 'json',
    names: ['--json'],
    description: 'Print one machine-readable JSON document.',
    commands: REPORT_COMMANDS,
  },
  {
    key: 'sarif',
    names: ['--sarif'],
    description: 'Print SARIF 2.1.0 (check and audit only; not with --json or --build).',
    commands: ['check', 'audit'],
  },
  {
    key: 'environment',
    names: ['--environment', '--env'],
    value: '<name>',
    description: 'Exact named Wrangler environment (deploy only; production is blocked).',
    commands: ['deploy'],
  },
  {
    key: 'execute',
    names: ['--execute'],
    description: 'Perform a real Cloudflare Workers deploy (requires --yes).',
    commands: ['deploy'],
  },
  {
    key: 'yes',
    names: ['--yes', '-y'],
    description: 'Confirm the real remote deploy (requires --execute).',
    commands: ['deploy'],
  },
]);

export const OPTION_BY_NAME = new Map(
  OPTION_DEFINITIONS.flatMap((definition) => definition.names.map((name) => [name, definition])),
);

export const CONTROL_OPTIONS = Object.freeze([
  { names: ['-h', '--help'], key: 'help', description: 'Show help and exit.' },
  { names: ['-V', '--version'], key: 'version', description: 'Print version and exit.' },
]);

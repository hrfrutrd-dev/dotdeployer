import path from 'node:path';
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js';
import { PROVIDERS, VERSION } from './constants.js';
import { checkProject } from './check.js';
import { runLocalBuild } from './build.js';
import { emulateProject } from './emulation.js';
import { auditProject } from './audit.js';
import { verifyProject } from './deployment.js';
import { providerInfo } from './providers/index.js';
import { withProjectSource } from './source.js';

const providerEnum = ['auto', 'all', 'render', 'railway', 'vercel', 'cloudflare', 'gas', 'generic'];

const TOOLS = [
  {
    name: 'preflight_check',
    description: 'Run a read-only local deployment preflight. Enforces the 20 MB per-file hard limit before reading provider configs. Supports Render, Railway, Vercel, Cloudflare Wrangler, and Google Apps Script. A successful result is not a cloud runtime guarantee.',
    inputSchema: {
      type: 'object',
      properties: {
        path: { type: 'string', description: 'Local project directory. Defaults to the current working directory.' },
        repository: { type: 'string', description: 'Optional GitHub URL or owner/repository shorthand to clone for the check.' },
        ref: { type: 'string', description: 'Optional branch or tag (only valid with repository).' },
        provider: { type: 'string', enum: providerEnum, description: 'Provider adapter to check; defaults to auto-detection.' },
        plan: { type: 'string', description: 'Assumed plan profile, such as free or paid. Defaults to free.' },
        strict: { type: 'boolean', description: 'Treat all warnings as errors.' },
      },
      additionalProperties: false,
    },
  },
  {
    name: 'audit_project',
    description: 'Read-only deployment security audit. Scans deployable text files for high-confidence committed credential patterns and derives an environment-variable contract from source, .env.example-style files, and supported provider config. It never executes project code, contacts providers, or returns matched secret values.',
    inputSchema: {
      type: 'object',
      properties: {
        path: { type: 'string', description: 'Local project directory. Defaults to the current working directory.' },
        repository: { type: 'string', description: 'Optional GitHub URL or owner/repository shorthand to clone for the audit.' },
        ref: { type: 'string', description: 'Optional branch or tag (only valid with repository).' },
        provider: { type: 'string', enum: providerEnum, description: 'Provider adapter to check; defaults to auto-detection.' },
        plan: { type: 'string', description: 'Assumed plan profile, such as free or paid. Defaults to free.' },
        strict: { type: 'boolean', description: 'Treat audit and preflight warnings as errors.' },
      },
      additionalProperties: false,
    },
  },
  {
    name: 'verify_provider_access',
    description: 'Run local preflight and credential scanning, then invoke the selected provider CLI identity command (whoami or clasp login --status). Identity text and raw CLI output are suppressed. This is a read-only live access check and does not deploy.',
    inputSchema: {
      type: 'object',
      properties: {
        path: { type: 'string', description: 'Local project directory. Defaults to the current working directory.' },
        repository: { type: 'string', description: 'Optional GitHub repository to clone for source preflight; provider auth still uses the local CLI account.' },
        ref: { type: 'string', description: 'Optional branch or tag (only valid with repository).' },
        provider: { type: 'string', enum: providerEnum, description: 'Exactly one provider adapter to verify.' },
        plan: { type: 'string', description: 'Assumed plan profile. Defaults to free.' },
        strict: { type: 'boolean', description: 'Treat all preflight and audit warnings as errors.' },
        timeoutSeconds: { type: 'integer', minimum: 1, maximum: 1800, description: 'Timeout for provider CLI probes. Defaults to 60 seconds.' },
      },
      required: ['provider'],
      additionalProperties: false,
    },
  },
  {
    name: 'run_local_build',
    description: 'Opt-in build test in a temporary copy of the deployable project context. May execute repository scripts and, when install=true, download packages and run lifecycle scripts. Use only for repositories you trust. This does not deploy to a hosting provider.',
    inputSchema: {
      type: 'object',
      properties: {
        path: { type: 'string', description: 'Local project directory. Defaults to the current working directory.' },
        repository: { type: 'string', description: 'Optional GitHub URL or owner/repository shorthand to clone for the build.' },
        ref: { type: 'string', description: 'Optional branch or tag (only valid with repository).' },
        provider: { type: 'string', enum: providerEnum, description: 'Provider adapter to check; defaults to auto-detection.' },
        plan: { type: 'string', description: 'Assumed plan profile. Defaults to free.' },
        install: { type: 'boolean', description: 'Install dependencies from a committed lockfile before building.' },
        timeoutSeconds: { type: 'integer', minimum: 1, maximum: 1800, description: 'Per-command timeout. Defaults to 600 seconds.' },
        strict: { type: 'boolean', description: 'Treat all preflight warnings as errors.' },
      },
      additionalProperties: false,
    },
  },
  {
    name: 'emulate_project',
    description: 'Run the provider-owned local validator/compiler in a temporary deploy-context copy when available. May execute project build code and download dependencies; never runs a deploy command. Requires Render, Railway Railpack, or Vercel CLI as applicable; Cloudflare Workers require Wrangler while Pages runs the configured local build. Google Apps Script has no official local runtime and returns unsupported.',
    inputSchema: {
      type: 'object',
      properties: {
        path: { type: 'string', description: 'Local project directory. Defaults to the current working directory.' },
        repository: { type: 'string', description: 'Optional GitHub URL or owner/repository shorthand to clone for emulation.' },
        ref: { type: 'string', description: 'Optional branch or tag (only valid with repository).' },
        provider: { type: 'string', enum: ['render', 'railway', 'vercel', 'cloudflare', 'gas'], description: 'Exactly one provider is required.' },
        plan: { type: 'string', description: 'Assumed plan profile. Defaults to free.' },
        install: { type: 'boolean', description: 'Install lockfile dependencies for Render/Pages local builds and Wrangler Workers dry-run. Vercel and Railpack manage their own installs.' },
        timeoutSeconds: { type: 'integer', minimum: 1, maximum: 1800, description: 'Per-command timeout. Defaults to 600 seconds.' },
        strict: { type: 'boolean', description: 'Treat all preflight warnings as errors.' },
      },
      required: ['provider'],
      additionalProperties: false,
    },
  },
  {
    name: 'list_providers',
    description: 'List supported provider adapters and their current local-validation scope.',
    inputSchema: { type: 'object', properties: {}, additionalProperties: false },
  },
];

function textResult(value, isError = false) {
  return {
    content: [{ type: 'text', text: JSON.stringify(value, null, 2) }],
    structuredContent: value,
    isError,
  };
}

function checkArguments(args) {
  const value = args ?? {};
  if (typeof value.path !== 'undefined' && typeof value.path !== 'string') throw new Error('path must be a string.');
  if (typeof value.repository !== 'undefined' && typeof value.repository !== 'string') throw new Error('repository must be a string.');
  if (typeof value.ref !== 'undefined' && typeof value.ref !== 'string') throw new Error('ref must be a string.');
  if (value.ref && !value.repository) throw new Error('ref requires repository.');
  if (value.repository && value.path !== undefined) throw new Error('Use path or repository, not both.');
  if (value.provider !== undefined && !providerEnum.includes(value.provider)) throw new Error(`Unsupported provider: ${value.provider}`);
  if (value.plan !== undefined && (typeof value.plan !== 'string' || !value.plan.trim())) throw new Error('plan must be a non-empty string.');
  if (value.strict !== undefined && typeof value.strict !== 'boolean') throw new Error('strict must be a boolean.');
  if (value.install !== undefined && typeof value.install !== 'boolean') throw new Error('install must be a boolean.');
  if (value.timeoutSeconds !== undefined && (!Number.isInteger(value.timeoutSeconds) || value.timeoutSeconds < 1 || value.timeoutSeconds > 1800)) throw new Error('timeoutSeconds must be an integer from 1 to 1800.');
  return value;
}

export async function startMcpServer() {
  const server = new Server(
    { name: 'dotdeployer', version: VERSION },
    { capabilities: { tools: {} } },
  );

  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));
  server.setRequestHandler(CallToolRequestSchema, async (request) => {
    const name = request.params.name;
    try {
      if (name === 'list_providers') return textResult({ providers: providerInfo() });
      const args = checkArguments(request.params.arguments);
      if (name === 'preflight_check') {
        let report;
        await withProjectSource({
          projectRoot: path.resolve(args.path ?? '.'),
          repository: args.repository,
          ref: args.ref,
        }, async (projectRoot) => {
          report = await checkProject({
            projectRoot,
            provider: args.provider ?? 'auto',
            plan: args.plan ?? 'free',
            strict: args.strict ?? false,
          });
          if (args.repository) report.projectRoot = args.repository;
        });
        return textResult(report, !report.ok);
      }
      if (name === 'audit_project') {
        let report;
        await withProjectSource({
          projectRoot: path.resolve(args.path ?? '.'),
          repository: args.repository,
          ref: args.ref,
        }, async (projectRoot) => {
          report = await auditProject({
            projectRoot,
            provider: args.provider ?? 'auto',
            plan: args.plan ?? 'free',
            strict: args.strict ?? false,
          });
          if (args.repository) {
            report.projectRoot = args.repository;
            report.preflight.projectRoot = args.repository;
          }
        });
        return textResult(report, !report.ok);
      }
      if (name === 'verify_provider_access') {
        let report;
        await withProjectSource({
          projectRoot: path.resolve(args.path ?? '.'),
          repository: args.repository,
          ref: args.ref,
        }, async (projectRoot) => {
          report = await verifyProject({
            projectRoot,
            provider: args.provider,
            plan: args.plan ?? 'free',
            strict: args.strict ?? false,
            timeoutSeconds: args.timeoutSeconds ?? 60,
          });
          if (args.repository) {
            report.projectRoot = args.repository;
            report.preflight.projectRoot = args.repository;
          }
        });
        return textResult(report, !report.ok);
      }
      if (name === 'run_local_build') {
        let report;
        await withProjectSource({
          projectRoot: path.resolve(args.path ?? '.'),
          repository: args.repository,
          ref: args.ref,
        }, async (projectRoot) => {
          report = await runLocalBuild({
            projectRoot,
            provider: args.provider ?? 'auto',
            plan: args.plan ?? 'free',
            install: args.install ?? false,
            timeoutSeconds: args.timeoutSeconds ?? 600,
            strict: args.strict ?? false,
          });
          if (args.repository) report.preflight.projectRoot = args.repository;
        });
        return textResult(report, !report.preflight.ok || !report.success);
      }
      if (name === 'emulate_project') {
        let report;
        await withProjectSource({
          projectRoot: path.resolve(args.path ?? '.'),
          repository: args.repository,
          ref: args.ref,
        }, async (projectRoot) => {
          report = await emulateProject({
            projectRoot,
            provider: args.provider,
            plan: args.plan ?? 'free',
            install: args.install ?? false,
            timeoutSeconds: args.timeoutSeconds ?? 600,
            strict: args.strict ?? false,
          });
          if (args.repository) {
            report.projectRoot = args.repository;
            report.preflight.projectRoot = args.repository;
            if (report.localBuild?.preflight) report.localBuild.preflight.projectRoot = args.repository;
          }
        });
        return textResult(report, !report.ok);
      }
      return textResult({ error: `Unknown tool: ${name}` }, true);
    } catch (error) {
      return textResult({
        ok: false,
        error: {
          code: error?.code ?? 'MCP_TOOL_FAILED',
          message: error?.message ?? String(error),
        },
      }, true);
    }
  });

  const transport = new StdioServerTransport();
  await server.connect(transport);
}

export const VERSION = '0.2.0';

// 20 MB (decimal). Source/deploy-context files at or above this size are
// rejected before provider configuration parsing or build commands.
export const MAX_FILE_SIZE_BYTES = 20_000_000;

export const PROVIDERS = Object.freeze({
  render: {
    id: 'render',
    label: 'Render',
    description: 'Render Blueprint (render.yaml / render.yml)',
    configNames: ['render.yaml', 'render.yml'],
  },
  railway: {
    id: 'railway',
    label: 'Railway',
    description: 'Railway project config (railway.json / railway.toml)',
    configNames: ['railway.json', 'railway.toml'],
  },
  vercel: {
    id: 'vercel',
    label: 'Vercel',
    description: 'Vercel project config (vercel.json)',
    configNames: ['vercel.json'],
  },
  cloudflare: {
    id: 'cloudflare',
    label: 'Cloudflare Workers / Pages',
    description: 'Wrangler config (wrangler.toml / wrangler.json / wrangler.jsonc)',
    configNames: ['wrangler.toml', 'wrangler.json', 'wrangler.jsonc'],
  },
  gas: {
    id: 'gas',
    label: 'Google Apps Script',
    description: 'Apps Script manifest and clasp config',
    configNames: ['appsscript.json', '.clasp.json'],
  },
});

export const PROVIDER_ALIASES = Object.freeze({
  'cloudflare-workers': 'cloudflare',
  'cloudflare-pages': 'cloudflare',
  workers: 'cloudflare',
  pages: 'cloudflare',
  'apps-script': 'gas',
  'google-apps-script': 'gas',
});

export const IGNORED_DIRS_WITHOUT_GIT = new Set([
  '.git',
  '.hg',
  '.svn',
  'node_modules',
  '.venv',
  'venv',
  '__pycache__',
  '.next',
  '.nuxt',
  '.output',
  '.svelte-kit',
  '.turbo',
  '.cache',
  'coverage',
  'dist',
  'build',
]);

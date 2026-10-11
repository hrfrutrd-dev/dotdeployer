# dotdeployer

**English** | [日本語](README.ja.md)

A CLI / MCP server that **inspects the project that would actually be uploaded to Git, locally and strictly, before you deploy it to a hosting provider; audits committed credentials and environment-variable exposure; and optionally runs the real build command in a temporary copy.**

Supported adapters: Render, Railway, Vercel, Cloudflare Workers / Pages (Wrangler), Google Apps Script (GAS).

## Documentation

| Document | Contents |
|---|---|
| This page | Overview, installation, how each feature works, and its caveats |
| [CLI reference](cli.md) | Commands, options, exit codes, and output formats |
| [Provider reference](providers.md) | Per-provider checks, `emulate` runners, and finding codes |
| [Repository layout](architecture.md) | Directory structure, module roles, and how to extend the tool |
| [日本語ドキュメント](README.ja.md) | Japanese version of these pages |

> **Important:** this is not a cloud emulator that reproduces every hosting provider's runtime, configuration surface, plan, or quota. It cannot eliminate the gap between local and online environments, and it cannot guarantee that a production deploy will succeed. It is a tool that catches deploy-blocking problems early through static configuration analysis and local builds. Read [Scope and limitations](#scope-and-limitations) for what is and is not covered.

## Installation

Node.js 20 or newer is required.

Install the command globally from the GitHub repository:

```sh
npm install --global git+https://github.com/hrfrutrd-dev/dotdeployer.git
dotdeployer --help
```

For local development (or to run from a checkout):

```sh
git clone https://github.com/hrfrutrd-dev/dotdeployer.git
cd dotdeployer
npm ci
npm link
```

You can also invoke the checked-out entrypoint without `npm link`:

```sh
node ./bin/dotdeployer.js check /path/to/project --provider vercel
```

## Basic usage

```sh
# Auto-detect the target service from configuration files
dotdeployer check .

# State the target and the assumed plan explicitly
dotdeployer check . --provider render --plan free

# Clone a GitHub branch into a temporary directory and check it (public or private; existing Git credentials are used)
dotdeployer check --repo owner/repository --ref main --provider railway

# Machine-readable JSON, for CI or other tools
dotdeployer check . --provider cloudflare --json

# Treat warnings as failures too
dotdeployer check . --provider vercel --strict

# Statically audit the deploy context for credential and environment-variable risk
dotdeployer audit . --provider vercel

# Emit SARIF 2.1.0 for GitHub Code Scanning and similar tools
dotdeployer audit . --sarif > dotdeployer.sarif

# Run the real build command on a temporary copy, in addition to the checks
dotdeployer test . --provider vercel --build

# Install dependencies from the lockfile first, then build
dotdeployer test . --provider vercel --build --install --timeout 900

# Local build / dry-run through each provider's official CLI (the CLI must already be installed)
dotdeployer emulate . --provider cloudflare --timeout 900

# Local checks plus an authentication check for the selected official CLI (whoami output is suppressed)
dotdeployer verify . --provider cloudflare

# No real deploy: inspect, dry-run, and plan a Wrangler staging environment
dotdeployer deploy . --provider cloudflare --environment staging

# [REAL REMOTE OPERATION] Deploy only to a confirmed non-production Worker.
# Real execution requires an explicit --execute --yes, after checking the environment.
dotdeployer deploy . --provider cloudflare --environment staging --execute --yes
```

Unless `--build` is given, `check` and `test` only perform static checks of configuration and files. `emulate` runs local dry-runs / builds through official CLIs, and `deploy` performs authentication checks and a dry-run of the selected environment — dependency installation happens only when `--install` is passed explicitly. With no `--provider`, supported configuration files are auto-detected, and every match is statically checked. When a build is requested and multiple providers are detected, you must name one explicitly so the build target is unambiguous.

`audit` is a read-only extra check that never builds anything. With `--strict`, warnings — such as undocumented environment variables or credentials that may be exposed — become errors as well. `--sarif` works only with `check` or `audit` and cannot be combined with `--json`.

## Live verification and real deploys (experimental)

In addition to `check` / `audit`, `verify` runs the authentication check (`whoami` equivalent) of the official Render / Railway / Vercel / Wrangler / clasp CLI. CLI identity output and raw logs are never included in the report. It can confirm CLI authentication state only; it does not prove your permission on a specific service, the actual values of environment variables, or that the provider-side build will succeed.

`deploy` is **simulation-only by default**. For a single Cloudflare Workers Wrangler configuration it runs a local preflight, a secret audit, an authentication check, an upload-free dry-run of the named environment, and a size check of the generated artifact, then prints the commands it would run. It does not publish to the remote.

A real remote operation is performed only when all of the following hold:

- Both `--execute` and `--yes` are supplied explicitly.
- `--environment` names an environment that exists in the Wrangler configuration.
- The target is Cloudflare Workers, not Pages.
- The target environment name (or the Wrangler environment Worker name) contains no `prod` / `production` segment.
- Authentication, static checks, the security audit, and the Wrangler dry-run for that same environment all succeed.

```sh
# Simulation: nothing is published to the network
dotdeployer deploy . --provider cloudflare --environment staging

# Real operation: Wrangler publishes the Worker to the staging environment
dotdeployer deploy . --provider cloudflare --environment staging --execute --yes
```

Real deploys are an early implementation limited to a subset of providers and Worker environments. `deploy` execution for Render / Railway / Vercel / GAS, Cloudflare Pages, and production environments is intentionally unsupported. `--install` fetches dependencies from the lockfile inside the temporary copy and runs install scripts; use it only with code you trust. On timeout the CLI process is stopped, but operations the provider has already accepted cannot necessarily be undone. There is no post-deploy smoke test and no automatic rollback, so verify the target account and environment before running it.

### The 20 MB hard limit

If any **individual file in the deploy context is 20,000,000 bytes or larger**, the source-side check stops before configuration parsing and builds. This is a fixed per-file ceiling, not a total size limit. For Git repositories it covers tracked and untracked files, while anything excluded by `.gitignore` and similar rules is not part of the Git deploy context. Git LFS pointers cannot be measured, so they are reported as errors. Static publish directories known from configuration, the Vercel output directory, and Wrangler dry-run output are rescanned with the same limit after a build. Docker image layers and provider-internal hidden build artifacts are not covered.

## Deployment security audit and the environment-variable contract

`audit` reads **only the text files that would actually enter the Git deploy context**. It never executes project code, install scripts, or external CLIs. It looks for high-confidence credential patterns (private keys, AWS access key IDs, GitHub / GitLab / Slack tokens, Stripe secret keys, Google API keys, literal credentials in `.npmrc` / `.netrc`) and for file names such as private SSH keys and keystores.

- **Matched credential values are never printed** to stdout, JSON, or SARIF. Only the file name, line number, rule name, and remediation are returned.
- If `audit` reports an `error`, revoke or rotate the value and remove it from Git history as well; deleting it in the next commit is often not enough.
- Files detected as binary, files excluded from the deploy context by Git ignore rules, unknown credential formats, Git history, and external secret-manager values are out of scope. A PASS is therefore not proof that no secret exists.

In addition, static references to `process.env.NAME`, `import.meta.env.NAME`, `Deno.env.get("NAME")`, and `Bun.env.NAME` in JavaScript / TypeScript / Vue / Svelte / Astro are collected and compared against `.env.example`, `.env.sample`, `.env.template`, and the non-secret environment declarations in supported provider configuration. Undocumented variables are reported as warnings, and a public prefix (`NEXT_PUBLIC_`, `VITE_`, `REACT_APP_`, `NUXT_PUBLIC_`, `GATSBY_`, `EXPO_PUBLIC_`, `VUE_APP_`, `PUBLIC_`, `ASTRO_PUBLIC_`) attached to a password / token / secret / database URL becomes an error. Names that are intentionally public, such as `API_KEY`, still produce a warning, so review origin / referrer / API restrictions.

```sh
# Document the required key names without any values
printf 'DATABASE_URL=\nINTERNAL_API_URL=\n' > .env.example

dotdeployer audit . --provider vercel --strict

# Produce a file for a CI code-scanning uploader
dotdeployer audit . --sarif > dotdeployer.sarif
```

SARIF output includes file and line information for issues that have a position. Follow the instructions of your CI / code-scanning integration to upload it.

## Supported providers

| Service | Configuration read | Representative local checks |
|---|---|---|
| Render | `render.yaml` / `render.yml` | Blueprint YAML; services, service type, envVars, rootDir, static publish path. Under the Free profile, persistent disks are errors. |
| Railway | `railway.json` / `railway.toml` | JSON / TOML; representative build and deploy fields, healthcheck, restart policy, Dockerfile references. |
| Vercel | `vercel.json` | JSON; build / output directory, rewrites / redirects / headers, representative function types. With crons under the Free profile it warns that current plan limits must be verified separately. |
| Cloudflare Workers / Pages | `wrangler.toml` / `wrangler.json` / `wrangler.jsonc` | TOML / JSON / JSONC; main / assets paths, vars, build, compatibility date. A missing compatibility date is an error under local policy. |
| Google Apps Script | `appsscript.json` / `.clasp.json` | Manifest, `rootDir`, runtime, OAuth scopes, representative Web App settings. `.gs` files are parsed for JavaScript syntax during a build, but the Apps Script API is never called. |
| Common | `package.json`, lockfiles, `Dockerfile`, deployable text | JSON / scripts / `engines.node` / `packageManager` types and lockfile consistency, Dockerfile `FROM`, and the high-confidence credential and environment-variable contract checks performed by `audit`. |

See the [provider reference](providers.md) for per-provider finding codes and limits.

### `emulate` with official local runners

Static rules alone cannot reproduce a provider's build closely enough, so `dotdeployer emulate --provider ...` invokes each provider's official CLI wherever one exists. When no official CLI exists, the command fails instead of reporting PASS based on an approximation of our own.

| Target | Local check performed |
|---|---|
| Render | `render blueprints validate` plus the Blueprint build command. Requires the Render CLI. |
| Railway | `railpack build` (Docker build when the Dockerfile builder is used). Requires Railpack / Docker. |
| Vercel | `vercel build`. Run `vercel link` beforehand so `.vercel/project.json` exists. The `--yes` flag that pulls environment variables is never run automatically. |
| Cloudflare Workers | `wrangler deploy --dry-run --outdir ...` — an official-bundler dry-run that does not deploy. |
| Cloudflare Pages | Runs the configured local build. This is not an upload-free Pages deploy dry-run, so the report states the execution level explicitly. |
| GAS | There is no official local runtime, so it fails as an emulator. Manifest and syntax checks remain available through `check` / `test`. |

Validating external CLIs may require read access to provider APIs and authentication, depending on the feature. Builds use the network to fetch dependencies and container images, and Railpack may use Docker CLI registry credentials. Build code itself is executed, but dotdeployer **never invokes deploy or push commands**. Even when Vercel or Railway credentials are configured, it does not run `vercel pull` or `railway up`.

```sh
# Example: Wrangler's official dry-run
dotdeployer emulate ./worker --provider cloudflare
```

This mode does not replicate production managed runtimes, bindings, secrets, or quotas either. The execution level is returned as `emulationLevel` in JSON, and anything not reproduced is returned in `limitations`.

`--plan free` / `--plan paid` select a check-policy profile. Arbitrary names such as `--plan hobby` or `--plan pro` are accepted, but the tool **does not query provider APIs and does not reproduce the current features or quotas of every plan**. Items where the free / paid difference is not explicitly checked are reported as warnings or as unverified. Plan terms change, so confirm against each provider's current documentation and a real account before going to production.

## MCP server

`dotdeployer mcp` starts an MCP stdio server. MCP clients such as Claude Code can use the read-only `preflight_check`, `audit_project` (which inspects credentials and environment variables), `verify_provider_access` (which checks official-CLI authentication), the opt-in `run_local_build`, `emulate_project` (official CLIs), and `list_providers`. The project-inspecting tools accept a GitHub `repository` and an optional `ref`. `verify_provider_access` runs a whoami-equivalent command but never returns account identity or raw logs. `audit_project` never executes project code and never returns a detected secret value. `emulate_project` requires the corresponding CLI and never reports success by pretending to reproduce a runtime it does not support. No real-deploy tool is exposed over MCP; if a remote operation is needed, use the CLI's explicit `deploy --execute --yes` after confirming the target and environment.

Example Claude Code MCP configuration (replace `/absolute/path/to/dotdeployer`):

```json
{
  "mcpServers": {
    "dotdeployer": {
      "command": "node",
      "args": ["/absolute/path/to/dotdeployer/bin/dotdeployer.js", "mcp"]
    }
  }
}
```

`run_local_build` and the CLI's `--build` execute commands from the target repository. With `--install` they fetch dependencies from the network and run dependency lifecycle scripts. **Use them only with code you trust.** Builds happen in a copy inside a temporary directory, but that is not an OS-level security sandbox: host permissions, environment variables, and network access are available.

## GitHub and configuration loading

`--repo` accepts a GitHub `owner/repository`, an HTTPS URL, or an SSH URL. dotdeployer never stores or asks for credentials and uses your existing Git credential helper / SSH agent. After cloning, the Git deploy context is scanned, the size limit is enforced, and only then is configuration parsed. Subdirectory projects can be pointed at directly with `--path`.

Configuration files are parsed with YAML / TOML / JSON / JSONC parsers. That does not mean an exact match with every historical schema version of every provider, and some new or experimental fields are unverified.

## Scope and limitations

- `check` performs local configuration and file inspection only. It never calls deploy APIs, never verifies authentication, and never uploads to a hosting environment.
- `audit` inspects only high-confidence patterns in deployable text files and static environment-variable references. It does not cover secret history, binary or ignored files, obfuscated or split values, variables built dynamically at runtime, or the actual values inside a provider secret manager.
- `--build` runs one of: the build command from configuration, the `build` script in `package.json`, or a Dockerfile build, on a temporary copy. It does not reproduce every provider's install / build / runtime / healthcheck sequence.
- Managed Node / Python / Go / Java runtimes, networking, DNS, regions, environment variables and secrets, OAuth consent, service-to-service communication, databases, cold starts, CPU / memory / execution-time / cron / API quotas, and similar details cannot be reproduced faithfully.
- Only known constraints that are statically decidable under the free profile are enforced. Quotas and contract terms that change frequently are reported as warnings or left unverified rather than guessed at with an error.
- `--strict` promotes known warnings to errors. It does not contact providers to discover new facts.
- `--build` runs real build scripts. Run it only when the repository's code is trusted.

A `PASS` therefore means "the local checks this tool performs have passed" — it does not mean "the production deploy will certainly succeed".

## Development

```sh
npm ci                 # reproducible dependency install
npm test               # the whole node:test suite
node ./bin/dotdeployer.js audit . --provider generic
node ./bin/dotdeployer.js --help
```

Tests live next to the modules they cover: `test/commands/` mirrors `src/commands/`, and `test/project/` covers input scanning and preflight behaviour in `src/project/`. See [Repository layout](architecture.md) for the directory structure and the role of each module.

When checking this repository itself, pass `--provider generic`. It contains no hosting provider configuration files, so a bare auto-detect run reports a single `NO_PROVIDER_CONFIG_DETECTED` info item (still a PASS).

## License

MIT — see [LICENSE](../LICENSE).

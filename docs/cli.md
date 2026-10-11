# CLI reference

**English** | [日本語](cli.ja.md)

← [Documentation](README.md)

Every command, option, exit code, and output format of the `dotdeployer` CLI.

## Invocation

```sh
# After a global install (or npm link)
dotdeployer <command> [path] [options]

# Straight from a checkout
node ./bin/dotdeployer.js <command> [path] [options]

# Global help and version
dotdeployer
dotdeployer --help
dotdeployer --version

# Command-specific help
dotdeployer check --help
dotdeployer help deploy
```

An unknown command, unsupported option, or invalid option combination prints usage to stderr and exits with code `2`. Use `--json` on report commands to receive usage errors as a JSON object on stdout.

## Commands

| Command | Purpose | Runs provider/network code |
|---|---|---|
| `check [path]` | Static preflight of configuration and deploy-context files. | No |
| `audit [path]` | Credential and environment-variable contract audit. Read-only, never executes project code. | No |
| `test [path]` | Same preflight as `check`; with `--build`, also runs the real build command on a temporary copy. | Only with `--build` (and `--install`) |
| `emulate [path] --provider <name>` | Local build / validation through the provider's official CLI where one exists. Never deploys. | Yes (requires the official CLI) |
| `verify [path] --provider <name>` | Local checks plus an official-CLI authentication check. Account identity output is suppressed. | Yes (requires the official CLI) |
| `deploy [path] --provider cloudflare --environment <name>` | Simulation by default; a real, non-production Workers deploy only with `--execute --yes`. | Only when `--execute --yes` is given |
| `providers [--json]` | List the supported provider adapters and their configuration formats. | No |
| `mcp` | Start the MCP server over stdio for Claude Code and other agents. | No |
| `help [command]` | Show global usage or help for one command. Also available as `<command> --help`. | No |

## Options

| Option | Value | Default | Applies to | Notes |
|---|---|---|---|---|
| `[path]` | Directory | `.` | check, audit, test, emulate, verify, deploy | Positional path. Cannot be combined with `--path` or `--repo`. |
| `--path <dir>` | Directory | `.` | check, audit, test, emulate, verify, deploy | Cannot be combined with a positional path or `--repo`. |
| `--repo <owner/repo\|URL>` | GitHub repo | – | check, audit, test, emulate, verify | Clones into a temporary directory. Rejected by `deploy`. |
| `--ref <branch-or-tag>` | Git ref | default branch | with `--repo` | `--ref` requires `--repo`. |
| `--provider <name>` | see below | `auto` | check, audit, test, emulate, verify, deploy | `verify` needs exactly one provider; `deploy` supports `cloudflare` only. |
| `--plan <name>` | Arbitrary label | `free` | check, audit, test, emulate, verify, deploy | A local policy profile, not a provider API lookup. |
| `--strict` | flag | off | check, audit, test, emulate, verify, deploy | Promotes every warning to an error. |
| `--build` | flag | off | check, test only | Runs the build command on a temporary copy. |
| `--install` | flag | off | with `--build`, or with `emulate` / `deploy` | Installs dependencies from the lockfile inside the temporary copy and runs dependency install scripts. |
| `--timeout <seconds>` | 1–1800 | 600 (`verify`: 60) | check, test, emulate, verify, deploy | Per-command timeout for any build/runner step. Out-of-range values are usage errors. |
| `--json` | flag | off | report commands and `providers` (not `mcp`) | Machine-readable single JSON document; usage errors are also JSON on stdout. |
| `--sarif` | flag | off | check, audit only | SARIF 2.1.0. Cannot be combined with `--json` or `--build`. |
| `--environment <name>`, `--env` | Wrangler env name | – | deploy only | Must exist in the Wrangler configuration and must not be production. |
| `--execute` | flag | off | deploy only | Performs the real remote deploy. Requires `--yes`. |
| `--yes`, `-y` | flag | off | deploy only | Confirms the real deploy. Requires `--execute`. |
| `-h`, `--help` | flag | – | all | Print usage and exit 0. |
| `-V`, `--version` | flag | – | all | Print the version and exit 0. |

`--provider` accepts `auto`, `all`, `render`, `railway`, `vercel`, `cloudflare`, `gas`, and `generic`. Aliases are resolved to canonical names: `cloudflare-workers`, `cloudflare-pages`, `workers`, and `pages` → `cloudflare`; `apps-script` and `google-apps-script` → `gas`.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | The checks passed (for `test`, the preflight passed and the build succeeded when requested). |
| `1` | The report is not `ok` (errors, or warnings promoted by `--strict`; for `test`, also a failed or missing build). |
| `2` | Usage error (unknown option, invalid combination, bad `--timeout`) or an unexpected internal error. With `--json`, the error is printed as a JSON object on stdout instead of a message on stderr. |

## Output formats

**Human-readable (default).** A short report plus one line per finding in the form `✖|⚠|ℹ [CODE] <file>: message`, where audit findings also carry `<file>:<line>`, with a `Fix:` hint when a remediation exists. Every check-style report ends with a reminder that a PASS is a local preflight, not a guarantee of cloud behaviour. `audit` additionally prints a line stating that secret values are never printed.

**`--json`.** A single JSON document per command. Highlights:

| Command | Top-level keys |
|---|---|
| `check` | `version`, `projectRoot`, `provider`, `detectedProviders`, `plan`, `strict`, `ok`, `filesScanned`, `maxFileSizeBytes`, `summary`, `issues` |
| `audit` | the `check` keys (minus `ok`/`summary` meaning) plus `preflight`, `summary`, `audit`, `environment`, `auditIssues` |
| `test` | `preflight`, `attempted`, `success`, `commands`, `issues` |
| `emulate` | `version`, `projectRoot`, `provider`, `plan`, `preflight`, `emulationLevel`, `limitations`, `runner`, `commands`, `localBuild`, `issues`, `success`, `ok` |
| `verify` | `version`, `projectRoot`, `provider`, `mode: "live-verification"`, `preflight`, `audit`, `authentication`, `commands`, `issues`, `ok` |
| `deploy` | `version`, `projectRoot`, `provider`, `environment`, `mode` (`simulation` / `execute`), `deployed`, `ok`, `verification`, `environmentDryRunPassed`, `commands`, `issues` |
| `providers` | `providers`, `fileSizeLimitBytes` |

`summary` is always `{ "errors": n, "warnings": n, "infos": n }`. `audit.audit` reports `filesInContext`, `scannedTextFiles`, `skippedBinaryFiles`, `scannedBytes`, `secretFindings`, and `findingsTruncated`. `environment` reports `referenced`, `documented`, `configured`, and the per-variable `variables` array (name, public, sensitivity, documented, configured, sources). `authentication` is `{ checked, authenticated, identityOutputSuppressed }`.

**`--sarif`.** SARIF 2.1.0 JSON for `check` or `audit`, suitable for GitHub Code Scanning and similar consumers. Findings carry `file` / `line` locations where a position exists, and matched secret values are never included:

```sh
dotdeployer audit . --provider vercel --sarif > dotdeployer.sarif
```

## Command details

### `check`

Parses every detected or explicitly selected provider configuration, validates paths that the configuration references, enforces the 20 MB per-file limit before any parsing, and runs the common project checks (`package.json`, lockfiles, `Dockerfile`). It never contacts a provider. If the size limit stops the run, the report sets `stoppedBeforeConfigValidation`.

### `audit`

Runs the full `check` preflight and adds the secret and environment-variable audit. It never executes project code. Values matching high-confidence credential patterns are reported by file, line, and rule name only. Undocumented environment variables are warnings, and become errors under `--strict`; a sensitive name behind a public prefix (`VITE_`, `NEXT_PUBLIC_`, …) is an error from the start.

### `test`

Runs `check` and, with `--build`, resolves a build command in this order: the provider configuration's build command, then the `build` script in `package.json`, then a Dockerfile build. The command runs inside a temporary copy of the deploy context; a build that fails, times out, or was never resolved fails the command. Generated output directories known from configuration are rescanned against the size limit afterwards. Add `--install` to install dependencies from the lockfile first.

### `emulate`

Deterministic per provider (see the [provider reference](providers.md)):

| Provider | Commands run | `emulationLevel` |
|---|---|---|
| `render` | `render --version`, `render blueprints validate <config>`, then the Blueprint build command | `official-blueprint-validation-plus-local-build` |
| `railway` | `railpack --version`, `railpack build …`, or `docker build` for the Dockerfile builder | `local-railpack-image-build` / `local-docker-build` |
| `vercel` | `vercel --version`, `vercel build` (requires a prior `vercel link`) | `official-vercel-build-output-api` |
| `cloudflare` (Workers) | `wrangler --version`, `wrangler deploy --dry-run --outdir <tmp>` | `official-wrangler-dry-run` |
| `cloudflare` (Pages) | the configured local build only | `local-pages-build-only` |
| `gas` | none available; fails as an emulator | – |

Missing CLIs are errors, not skipped steps. `limitations` in the JSON output lists what the run did not reproduce.

### `verify`

Runs `audit` and then the provider CLI authentication probe:

| Provider | Probe |
|---|---|
| `render` | `render whoami` |
| `railway` | `railway whoami` |
| `vercel` | `vercel whoami` |
| `cloudflare` | `wrangler whoami` |
| `gas` | `clasp login --status` |

Each probe is preceded by `<cli> --version`. Identity output and raw logs are deliberately excluded from the report, and `authentication.identityOutputSuppressed` is always `true`. Without exactly one supported provider the run fails with `VERIFY_PROVIDER_REQUIRED`.

### `deploy`

Simulation first. The sequence is local preflight → secret audit → authentication check → upload-free dry-run of the named environment → artifact size check. A real deploy additionally requires `--execute --yes`, a Wrangler environment declared in the configuration, a Worker (not Pages), a non-production environment name, and success of every earlier step. Blocking codes include `DEPLOY_PROVIDER_UNSUPPORTED`, `DEPLOY_ENVIRONMENT_REQUIRED`, `DEPLOY_CONFIRMATION_REQUIRED`, `PAGES_DEPLOY_UNSUPPORTED`, `WRANGLER_ENVIRONMENT_NOT_CONFIGURED`, and `PRODUCTION_DEPLOY_BLOCKED`. In simulation mode the publish command is listed with `"planned": true` instead of being run.

### `providers`

Prints the adapter list, or the same data as JSON with `--json` (`providers`, `fileSizeLimitBytes`). The free / paid distinction is a policy label, not a reproduction of current vendor quotas.

### `mcp`

Starts the stdio MCP server (`preflight_check`, `audit_project`, `verify_provider_access`, `run_local_build`, `emulate_project`, `list_providers`). No deploy tool is exposed. See the [documentation](README.md#mcp-server) for a client configuration example.

## Cookbook

```sh
# CI gate: fail on any finding, including warnings
dotdeployer check . --provider vercel --strict --json

# Code Scanning upload
dotdeployer audit . --provider vercel --sarif > dotdeployer.sarif

# Review someone else's branch without cloning it yourself
dotdeployer audit --repo owner/repository --ref feature/deploy --provider railway

# Full local reproduction of the build, with a longer timeout
dotdeployer test . --provider render --build --install --timeout 1800

# Confirm credentials exist before touching the provider, without printing them
dotdeployer verify . --provider cloudflare

# Dry-run a staging Worker, then decide whether to deploy
dotdeployer deploy . --provider cloudflare --environment staging
dotdeployer deploy . --provider cloudflare --environment staging --execute --yes
```

## See also

- [Documentation](README.md) — concepts, security model, scope and limitations
- [Provider reference](providers.md) — per-provider checks and finding codes
- [Repository layout](architecture.md) — where each command is implemented

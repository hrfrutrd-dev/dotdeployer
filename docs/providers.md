# Provider reference

**English** | [日本語](providers.ja.md)

← [Documentation](README.md)

What dotdeployer checks for each hosting provider, which official CLI it uses for `emulate`, and the finding codes you will see in reports.

## Adapters and configuration detection

| Provider | Configuration files | Static checks |
|---|---|---|
| `render` | `render.yaml`, `render.yml` | Blueprint services and service types, env vars and env groups, `rootDir`, Dockerfile references, static publish path, free-plan disk |
| `railway` | `railway.json`, `railway.toml` | `build` / `deploy` fields, builder name, Dockerfile references, healthcheck path and timeout, restart policy, replicas |
| `vercel` | `vercel.json` | Build / install / dev / output directory, `framework`, rewrites and redirects, headers, functions, crons, env keys |
| `cloudflare` | `wrangler.toml`, `wrangler.json`, `wrangler.jsonc` | `name`, `main`, `pages_build_output_dir`, `compatibility_date` and flags, `vars`, `build.command`, `assets.directory`, named environments |
| `gas` | `appsscript.json`, `.clasp.json` | clasp `rootDir`, manifest `timeZone`, `runtimeVersion`, `exceptionLogging`, `oauthScopes`, `webapp` access / execution |

Detection matches these **file names anywhere in the deploy context**, not only at the repository root:

- `--provider auto` (default) and `--provider all` check every detected provider. If nothing is detected, the run reports an info-level `NO_PROVIDER_CONFIG_DETECTED` and still performs the common checks.
- Naming a provider checks that adapter even when no configuration file exists: a warning-level `PROVIDER_CONFIG_MISSING` for Render, Railway, Vercel, and Cloudflare, and an error-level `GAS_MANIFEST_MISSING` for Apps Script.
- `--provider generic` skips provider configuration entirely and runs only the common project checks.
- Several configuration files for the same provider produce a `MULTIPLE_PROVIDER_CONFIGS` warning, and each file is checked independently.
- When a build is requested and more than one provider (or more than one provider configuration) is detected, the build target must be named explicitly: `BUILD_PROVIDER_AMBIGUOUS` / `BUILD_PROVIDER_CONFIG_AMBIGUOUS`.

## Checks shared by every provider

These run for all commands and all providers:

**Deploy context enumeration.** Files are taken from `git ls-files` (including untracked files) in a Git repository, or from a directory walk that ignores `.git`, `node_modules`, `.venv`, `__pycache__`, `.next`, `.nuxt`, `.output`, `.svelte-kit`, `.turbo`, `.cache`, `coverage`, `dist`, and `build` otherwise. Symbolic links that point outside the project, dangling links, and directory links fail with `EXTERNAL_SYMLINK`. Any file of 20,000,000 bytes or more stops the run with `FILE_TOO_LARGE` before any configuration is parsed; Git LFS pointers and Docker image layers are outside this check.

**`package.json` and lockfiles.** `PACKAGE_JSON_INVALID`, `PACKAGE_JSON_SHAPE_INVALID`, `PACKAGE_SCRIPTS_INVALID`, `PACKAGE_SCRIPT_INVALID`, `PACKAGE_TYPE_INVALID`, `PACKAGE_ENGINES_INVALID`, `PACKAGE_NODE_ENGINE_INVALID`, `PACKAGE_MANAGER_INVALID`, `PACKAGE_MANAGER_UNSUPPORTED`, and `PACKAGE_MANAGER_LOCK_MISSING` are errors. Recognized lockfiles are `package-lock.json`, `npm-shrinkwrap.json`, `pnpm-lock.yaml`, `yarn.lock`, `bun.lock`, and `bun.lockb`; conflicting managers produce a `MULTIPLE_LOCKFILES` warning and a missing lockfile produces a `LOCKFILE_MISSING` warning, which `--strict` promotes to an error.

**Environment files.** Any `.env*` file in the deploy context other than `.env.example`, `.env.sample`, `.env.template`, or `.env.defaults` produces an `ENV_FILE_IN_DEPLOY_CONTEXT` warning.

**Dockerfiles.** A `Dockerfile` without a `FROM` instruction is an error (`DOCKERFILE_FROM_MISSING`); unreadable files produce `DOCKERFILE_READ_ERROR`. Provider-specific Dockerfile references must exist *and* be part of the deploy context, otherwise `DOCKERFILE_NOT_IN_DEPLOY_CONTEXT`.

**Configuration parsing.** `CONFIG_PARSE_ERROR`, `CONFIG_SHAPE_INVALID`, `CONFIG_FIELD_REQUIRED`, `CONFIG_FIELD_TYPE`, `CONFIG_READ_ERROR`, and the path codes `CONFIG_PATH_ESCAPES_PROJECT`, `CONFIG_PATH_NOT_FILE`, `CONFIG_PATH_NOT_DIRECTORY`, `CONFIG_PATH_NOT_FOUND`, `CONFIG_PATH_CHECK_FAILED`. Paths that would escape the project directory are always errors.

## Plan profiles

`--plan` selects a local policy profile; the default is `free`. The names treated as "free" for each provider are:

| Provider | Names treated as free |
|---|---|
| `render` | `free` |
| `railway` | `free`, `trial` |
| `vercel` | `free`, `hobby` |
| `cloudflare` | `free`, `workers free` |
| `gas` | `free`, `consumer` |

Any other value (including `paid`, `pro`, `standard`) selects the paid profile. The profile only changes checks that are statically decidable:

| Finding | Severity | Meaning |
|---|---|---|
| `PLAN_LIMIT_DISK` | error on free | A Render service declares `disk`; the local free profile rejects persistent disks. |
| `PLAN_MISMATCH` | error on free | A Render service declares a non-`free` `plan` while the free profile is selected. |
| `PLAN_LIMIT_UNVERIFIED` | warning | A feature that may be plan-gated (Railway replicas > 1, Vercel crons, Cloudflare custom limits) — verify current terms with the provider. |
| `PLAN_QUOTAS_NOT_SIMULATED` | info | Apps Script quotas cannot be reproduced offline. |

dotdeployer never queries a provider API for plan data, and quotas, prices, and feature gates change frequently, so anything not statically decidable is reported as a warning or as unverified rather than as a hard failure.

## Render

- **Configuration:** `render.yaml` / `render.yml`. A Blueprint must define a non-empty `services` array (`RENDER_SERVICES_REQUIRED`).
- **Services:** each entry must be an object with a non-empty `name`, and `type` must be one of `static`, `web`, `worker`, `cron`, `pserv` (`RENDER_SERVICE_INVALID`, `RENDER_SERVICE_TYPE_INVALID`).
- **Paths:** `rootDir` must be relative and exist; `dockerfilePath` / `dockerContext` must exist; `staticPublishPath` must be relative to the service root and is reported as info when it does not exist yet (it may be created by the build).
- **Env vars:** `envVars` must be an array of objects with scalar values (`RENDER_ENV_VAR_INVALID`). A key matching secret / token / password / private key / API key produces a `POSSIBLE_SECRET_IN_CONFIG` warning, and a `fromGroup` that is not declared in the Blueprint produces an `ENV_GROUP_NOT_DECLARED` warning.
- **Runtime:** a service without an explicit `startCommand` or `dockerCommand` produces a `RENDER_START_COMMAND_UNVERIFIED` warning because Render may infer the command.
- **`emulate`:** requires the Render CLI. Runs `render --version`, `render blueprints validate <config>`, then the Blueprint's build command; `emulationLevel` is `official-blueprint-validation-plus-local-build`. Multi-service Blueprints fail closed rather than being partially built.
- **Limits:** Render's own build image, instance types, disks, and region behaviour are not reproduced.

## Railway

- **Configuration:** `railway.json` / `railway.toml`; `build` and `deploy` must be objects when present.
- **Build:** `builder` must be a string; unknown names produce a `RAILWAY_BUILDER_UNKNOWN` warning, and the known names for this checker are `DOCKERFILE` and `RAILPACK`. `buildCommand`, `dockerfilePath`, and `dockerContext` are validated, including existence and deploy-context membership.
- **Deploy:** `startCommand`, `preDeployCommand`, `healthcheckPath`, and `cronSchedule` must be strings; `healthcheckPath` must start with `/` (`RAILWAY_HEALTHCHECK_PATH_INVALID`); `healthcheckTimeout`, `restartPolicyMaxRetries`, and `numReplicas` must be integers; `restartPolicyType` must be `ON_FAILURE`, `ALWAYS`, or `NEVER` (`RAILWAY_RESTART_POLICY_INVALID`).
- **Runtime:** a configuration with neither `deploy.startCommand` nor a Dockerfile produces a `RAILWAY_START_COMMAND_UNVERIFIED` warning.
- **`emulate`:** requires Railpack or Docker. Runs `railpack --version` and `railpack build …`, or a Docker build when the Dockerfile builder is selected; `emulationLevel` is `local-railpack-image-build` or `local-docker-build`. A temporary image is removed afterwards, and a failed cleanup is reported as `DOCKER_IMAGE_CLEANUP_FAILED`.
- **Limits:** Railway's own managed build environment, registry credentials, volumes, and health monitoring are outside the local run.

## Vercel

- **Configuration:** `vercel.json`. String fields are `buildCommand`, `installCommand`, `devCommand`, `outputDirectory`, `framework`, and `ignoreCommand`; `cleanUrls` and `trailingSlash` must be booleans.
- **Routing:** `rewrites`, `redirects`, `headers`, `routes`, `builds`, and `crons` must be arrays. Rewrites and redirects need string `source` and `destination` (`VERCEL_ROUTE_INVALID`); header rules need a string `source` and an array of headers (`VERCEL_HEADER_RULE_INVALID`).
- **Functions:** keyed by glob, each entry must be an object whose `maxDuration` and `memory` are positive integers (`VERCEL_FUNCTION_CONFIG_INVALID`).
- **Output:** `outputDirectory` is reported as info when it does not exist yet, since the build may create it.
- **Crons:** with the free profile, a non-empty `crons` array produces `PLAN_LIMIT_UNVERIFIED`.
- **`emulate`:** requires the Vercel CLI and a prior `vercel link` (missing project link produces `VERCEL_PROJECT_NOT_LINKED`). Runs `vercel --version` and `vercel build`, then checks that `.vercel/output` exists (`VERCEL_OUTPUT_MISSING` / `VERCEL_OUTPUT_INVALID`); `emulationLevel` is `official-vercel-build-output-api`. The `--yes` flag that would pull environment variables is never added automatically.
- **Limits:** Vercel's Build Output API is present locally, but edge network behaviour, regions, and image optimisation are not reproduced.

## Cloudflare Workers / Pages

- **Configuration:** `wrangler.toml`, `wrangler.json`, or `wrangler.jsonc`, parsed as TOML, JSON, or JSONC (`WRANGLER_CONFIG_INVALID` on parse or shape failures).
- **Identity and entry points:** `name` must be a non-empty string when present; `main` must be a string and the file must exist (`CONFIG_PATH_NOT_FOUND`).
- **Compatibility:** `compatibility_date` must be a real `YYYY-MM-DD` calendar date (`CLOUDFLARE_COMPATIBILITY_DATE_INVALID`) and is **required** by this checker's policy (`CLOUDFLARE_COMPATIBILITY_DATE_REQUIRED`); `compatibility_flags` must be an array of strings.
- **Vars and assets:** `vars` must be an object with string values (`CLOUDFLARE_VAR_INVALID`), and keys that look sensitive produce `POSSIBLE_SECRET_IN_CONFIG` because `vars` are plain configuration. `assets.directory` and `pages_build_output_dir` are checked for existence, with info severity when they may be generated by a build.
- **Named environments:** every `env.<name>` object receives the same type, path, compatibility, vars, build, assets, and plan-profile checks. Fields in findings are prefixed with `env.<name>.`; malformed entries produce `CLOUDFLARE_ENVIRONMENT_INVALID`. Missing environment-level compatibility settings continue to inherit the required top-level setting.
- **Pages:** a project using `pages_build_output_dir` is treated as Pages. `emulate` runs only the configured local build (`emulationLevel: local-pages-build-only`) and `PAGES_RUNTIME_NOT_EMULATED` records that this is not an upload-free Pages deploy dry-run. The `deploy` command refuses Pages entirely (`PAGES_DEPLOY_UNSUPPORTED`).
- **`emulate` (Workers):** requires Wrangler. Runs `wrangler --version` and `wrangler deploy --dry-run --outdir <tmp>` from the configuration's directory; `emulationLevel` is `official-wrangler-dry-run`, and the generated output is size-scanned with the 20 MB limit. Assets-only Workers without `main` use this path too, while only an explicit Pages output setting selects the Pages build path.
- **`deploy`:** the only provider with an execution path. A single Wrangler configuration is required, the environment must be declared (`WRANGLER_ENVIRONMENT_NOT_CONFIGURED`, `WRANGLER_ENVIRONMENT_INVALID`), the target must not be Pages, and production environment or effective Worker names (including inherited names) are blocked (`PRODUCTION_DEPLOY_BLOCKED`). Without `--execute --yes` the publish command is only listed as planned.
- **Limits:** bindings (KV, D1, R2, queues, durable objects), service bindings, secrets, and Workers runtime semantics are not reproduced.

## Google Apps Script

- **Configuration:** `.clasp.json` and `appsscript.json`. A `rootDir` must be relative, inside the project, and exist; the manifest is looked up under that root, and a missing manifest is `GAS_MANIFEST_MISSING`.
- **Manifest:** `timeZone` is required; `runtimeVersion` must be `V8` or `DEPRECATED_ES5` (`GAS_RUNTIME_INVALID`), with a `GAS_RUNTIME_DEPRECATED` warning for ES5 and a `GAS_RUNTIME_UNPINNED` warning when it is absent; `exceptionLogging` must be `STACKDRIVER`, `CLOUD_LOGGING`, or `NONE`; `oauthScopes` must be an array of HTTPS URLs (`GAS_OAUTH_SCOPES_INVALID`).
- **Web Apps:** `webapp.access` must be `MYSELF`, `DOMAIN`, `ANYONE`, or `ANYONE_ANONYMOUS` (`GAS_WEBAPP_ACCESS_INVALID`); `webapp.executeAs` must be `USER_ACCESSING` or `USER_DEPLOYING` (`GAS_WEBAPP_EXECUTION_INVALID`).
- **Build:** there is no real build. With `--build`, `.gs` sources next to the manifest are parsed as JavaScript (`GAS_SYNTAX_OK`, `GAS_SYNTAX_ERROR`) and a project without sources produces a `GAS_SOURCE_NOT_FOUND` warning. The Apps Script API is never called.
- **`emulate`:** fails, because there is no official local runtime. Use `check` / `test` for manifest and syntax validation.
- **`verify`:** runs `clasp login --status` (and `clasp --version`) without printing identity output.
- **Limits:** OAuth consent, quotas, triggers, and container-bound behaviour cannot be reproduced offline.

## Secrets and the environment-variable contract

`audit` adds these checks. See the [documentation](README.md#deployment-security-audit-and-the-environment-variable-contract) for the security model.

| Finding | Severity | Trigger |
|---|---|---|
| `SECRET_PRIVATE_KEY` | error | A PEM private-key block in deployable text |
| `SECRET_AWS_ACCESS_KEY` | error | A value matching an AWS access key ID |
| `SECRET_GITHUB_TOKEN` | error | GitHub token patterns (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`) |
| `SECRET_GITLAB_TOKEN` | error | `glpat-` personal access tokens |
| `SECRET_SLACK_TOKEN` | error | `xoxa-`, `xoxb-`, `xoxp-`, `xoxr-`, `xoxs-`, or `xapp-` tokens |
| `SECRET_STRIPE_SECRET_KEY` | error | `sk_live_` / `sk_test_` secret keys and `rk_live_` / `rk_test_` restricted keys |
| `SECRET_OPENAI_API_KEY` | error | `sk-proj-`, `sk-svcacct-`, or `sk-admin-` API keys |
| `SECRET_GOOGLE_API_KEY` | error | `AIza…` API keys |
| `SECRET_URL_CREDENTIALS` | error | A URL containing an embedded username and non-placeholder password |
| `SECRET_ENVIRONMENT_VALUE` | error | A sensitive-looking variable with a literal value in a committed non-template `.env` file |
| `SECRET_NPM_TOKEN` | error | Literal `_authToken` values in `.npmrc` (a `${VAR}` reference is accepted) |
| `SECRET_NETRC_PASSWORD` | error | Literal `password` entries in `.netrc` |
| `CREDENTIAL_FILE_IN_DEPLOY_CONTEXT` | error | An `id_rsa` / `id_dsa` / `id_ecdsa` / `id_ed25519` private key file |
| `SENSITIVE_KEYSTORE_IN_DEPLOY_CONTEXT` | warning | A `.p12`, `.pfx`, or `.key` file |
| `SECRET_FINDINGS_TRUNCATED` | warning | The scan hit its finding cap, so results are incomplete |
| `ENVIRONMENT_VARIABLE_UNDOCUMENTED` | warning (error with `--strict`) | `process.env.X` / `import.meta.env.X` / `Deno.env.get("X")` / `Bun.env.X` with no `.env.example`-style or provider declaration |
| `SENSITIVE_ENVIRONMENT_EXPOSED_TO_CLIENT` | error | A sensitive name behind a public prefix |

Public prefixes recognised: `NEXT_PUBLIC_`, `VITE_`, `REACT_APP_`, `NUXT_PUBLIC_`, `GATSBY_`, `EXPO_PUBLIC_`, `VUE_APP_`, `PUBLIC_`, `ASTRO_PUBLIC_`. Names containing `PASSWORD`, `TOKEN`, `SECRET`, `PRIVATE_KEY`, `API_KEY`, `DATABASE_URL`, or similar words are treated as sensitive when they appear behind one of those prefixes.

Documented variables come from `.env.example`, `.env.sample`, `.env.template`, `.env.defaults`, and non-secret declarations in supported provider configuration. Configured variables come from provider configuration (`render.yaml` `envVars`, `vercel.json` `env`, `wrangler` `vars`). Provider-supplied runtime variables (`PATH`, `NODE_ENV`, `CI`, `VERCEL_*`, and similar) are recognised and not reported as undocumented.

## Finding severity

- **error** — fails the command (exit code 1). `--strict` does not change these.
- **warning** — reported but does not fail the command, unless `--strict` is used.
- **info** — context only; never affects the result.

## See also

- [CLI reference](cli.md) — commands, options, exit codes
- [Documentation](README.md) — scope and limitations
- [Repository layout](architecture.md) — where each adapter lives and how to add one

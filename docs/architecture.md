# Repository layout

**English** | [日本語](architecture.ja.md)

← [Documentation](README.md)

How the repository is organised, how a command flows through the code, and where to add a provider or a check.

## Top level

```
dotdeployer/
├── bin/dotdeployer.js        Executable entry point (loads src/cli and sets the exit code)
├── docs/                     Documentation, English and Japanese (see below)
├── src/                      Implementation (see below)
├── test/                     node:test suites, mirroring src/
├── .github/workflows/        CI: tests plus a strict self-audit
├── package.json              Dependencies, engines (>= Node 20), bin mapping
├── package-lock.json         Lockfile used by npm ci
├── LICENSE                   MIT
└── README.md                 Short entry page (English) linking into docs/
```

`test/` mirrors the source tree:

| Test | Covers |
|---|---|
| `test/commands/audit.test.js` | Secret rules, environment contract, SARIF output |
| `test/commands/deployment.test.js` | `verify` / `deploy` gating, CLI probes, size checks |
| `test/commands/emulation.test.js` | Official-CLI emulation paths and failure modes |
| `test/project/files.test.js` | Deploy-context scanning and temporary copies |
| `test/project/preflight.test.js` | `checkProject` behaviour per provider, builds, the 20 MB limit |

`docs/` holds one pair of files per topic; both languages must be updated together:

| English | Japanese |
|---|---|
| [`README.md`](README.md) | [`README.ja.md`](README.ja.md) |
| [`cli.md`](cli.md) | [`cli.ja.md`](cli.ja.md) |
| [`providers.md`](providers.md) | [`providers.ja.md`](providers.ja.md) |
| [`architecture.md`](architecture.md) | [`architecture.ja.md`](architecture.ja.md) |

## `src/`

```
src/
├── cli/index.js          Argument parsing, usage text, report printing, exit codes
├── commands/
│   ├── check.js          Static preflight: file scan -> generic + provider validation
│   ├── audit.js          Secret patterns and environment-variable contract
│   ├── build.js          Temporary copy, dependency install, build-command resolution
│   ├── emulation.js      Provider build/validation through official CLIs
│   └── deployment.js     verify (CLI auth probe) and deploy (guarded Workers publish)
├── core/
│   ├── constants.js      Version, provider registry, 20 MB limit, ignored directories
│   ├── errors.js         DotdeployerError, issue(), summarizeIssues()
│   └── output.js         Size scanning of generated build output
├── project/
│   ├── files.js          Deploy-context enumeration and per-file guards
│   ├── config.js         YAML / TOML / JSON / JSONC parsing and shape assertions
│   └── source.js         Local directory or temporary GitHub clone
├── providers/index.js    Provider detection and per-provider static validators
├── reporting/sarif.js    SARIF 2.1.0 conversion
└── mcp/server.js         MCP stdio server exposing the read-only tools
```

Layer responsibilities:

- **`cli/`** is the only module that writes to stdout/stderr and the only place that decides exit codes. It never contains validation logic.
- **`commands/`** implements one user-facing command each. Every function there is also the API used by the MCP server, so it must stay free of console output.
- **`core/`** holds primitives with no knowledge of providers or commands.
- **`project/`** knows how to turn "a project directory or a GitHub repository" into a list of deployable files and parsed configuration.
- **`providers/`** is pure static validation: no subprocesses, no network.
- **`reporting/`** converts an existing report into another format.

## How a command flows

1. `bin/dotdeployer.js` calls `main()` in `src/cli/index.js`.
2. `parseArgs()` validates the option combination (for example `--execute` requires `--yes`), returning exit code 2 for usage errors.
3. `withProjectSource()` (`src/project/source.js`) resolves the project: a local directory, or a temporary clone when `--repo` is used. The temporary clone is removed afterwards.
4. `scanProjectFiles()` (`src/project/files.js`) lists the deploy context: `git ls-files` including untracked files in a repository, or a filtered directory walk otherwise. It rejects links that escape the project and stops immediately if a file reaches the 20 MB limit — this happens **before** any configuration is parsed.
5. `checkProject()` (`src/commands/check.js`) runs `validateGenericProject()`, then each selected adapter from `src/providers/index.js`, using `parseConfigFile()` from `src/project/config.js`. Findings are collected with `issue()` and summarised with `summarizeIssues()`.
6. Depending on the command, one of the following runs on top of that preflight:
   - `auditProject()` — secret rules, filename rules, environment contract;
   - `runLocalBuild()` — copies the deploy context to a temporary directory, optionally installs from the lockfile, resolves one build command, runs it, then rescans generated output with `src/core/output.js`;
   - `emulateProject()` — runs the provider's official CLI, recording `emulationLevel` and `limitations`;
   - `verifyProject()` / `deployProject()` — CLI authentication probes, then an environment dry-run and, only with `--execute --yes`, a real publish.
7. `src/cli/index.js` prints the report (human text, `--json`, or SARIF via `src/reporting/sarif.js`) and returns the exit code.

## Invariants

These hold throughout the codebase; keep them when adding features.

1. **Secret values never reach output.** Rules report a file, line, rule id, and remediation only, and the test suite asserts that matched values are absent from human, JSON, and SARIF output.
2. **Provider identity never reaches output.** `verify` and `deploy` suppress whoami output and report only `{ checked, authenticated, identityOutputSuppressed }`.
3. **The size guard runs first.** No configuration is parsed and no subprocess is started until the deploy context passes the per-file limit. Violations set `stoppedBeforeConfigValidation`.
4. **Paths never escape the project.** Configuration references, symlink targets, build output directories, and Docker contexts are all resolved and checked.
5. **`check` and `audit` are offline and side-effect free.** Only `test --build/--install`, `emulate`, `verify`, and `deploy` start subprocesses, and only `deploy --execute --yes` performs a remote write.
6. **Findings are the machine contract.** Issue codes are stable identifiers, severities are `error` / `warning` / `info`, and `--strict` promotes warnings to errors at the check layer so that a single report shape reaches every consumer.
7. **The tool configures itself only through CLI flags.** There is no dotdeployer configuration file and no environment variable that changes check behaviour.

## Adding a provider

1. Add an entry to `PROVIDERS` in `src/core/constants.js` (id, label, description, `configNames`). Detection matches these names anywhere in the deploy context, so keep them exact and complete.
2. Write a validator in `src/providers/index.js` and register it in `validateProvider()`. Use `parseConfigFile()`, `assertObjectConfig()`, `validateString()`, `addTypeIssue()`, and `validatePathExists()` so parse, shape, and path errors stay consistent across providers.
3. Give every finding a stable code prefixed with the provider (for example `RAILWAY_HEALTHCHECK_PATH_INVALID`), a severity, and, where useful, a remediation string.
4. If the provider has an official local CLI, add an emulation branch in `src/commands/emulation.js` with an `emulationLevel` value describing exactly how far the local run goes. If it has no official CLI (as with GAS), fail rather than approximate.
5. If the provider has an authentication probe, add it to `ACCESS_PROBES` in `src/commands/deployment.js`.
6. Update [providers.md](providers.md) and [providers.ja.md](providers.ja.md), and add tests under `test/`.

## Adding a check

1. Emit findings with `issue(severity, CODE, message, file?, remediation?)` from `src/core/errors.js`; never print inside a command module.
2. Prefer `error` only for things that are certainly broken. Anything dependent on provider behaviour that changes over time belongs in `warning` or `info` so `--strict` remains meaningful.
3. Attach `file` (and `line` for text findings) so SARIF output can carry a location.
4. Add a test that asserts both the code and the severity, including the `--strict` promotion where relevant.

## Documentation rules

- Each English document has a Japanese counterpart with the same structure and the same tables. Update both in one change.
- Every document starts with a language switcher and links back to the documentation index.
- Keep command syntax, option names, and finding codes verbatim — they are part of the tool's interface.

# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-10-11

The first stable release: a complete, dependency-free dotfile deployment tool.

### Added

**Manifest and configuration**

- TOML, JSON and YAML manifests (`dotdeployer.toml`/`.json`/`.yaml`) discovered in the current
  directory or any parent, plus `include`, per-host (`hosts/<hostname>.toml`) and machine-local
  (`local.toml`) layers, merged in a documented order.
- Entries with modes `symlink`, `copy`, `template`, `mkdir` and `ignore`; shorthand tables
  (`[files]`, `[symlinks]`, `[copies]`, `[templates]`, `[dirs]`) and per-entry keys for
  permissions, ownership, tags, profiles, platforms, hosts, conditions, per-entry variables,
  text normalisation and temporary disabling.
- Conflict strategies per entry: `backup` (default), `override`, `skip`, `ask` and `merge`.
- Profiles with variables, extra entries, tags, hooks, settings and `extends`, selectable with
  `-p/--profile`, `DOTDEPLOYER_PROFILE` or `--activate`-style helpers.
- Conditions evaluated against system facts: `==`, `!=`, comparison operators, `and`/`or`/`not`,
  `in`/`not in`, `matches`, truthiness, and helper calls such as `has()`, `which()`, `env()`.

**Templating**

- Built-in template engine (no Jinja2 dependency) with `{{ expressions }}`, `{% if %}`,
  `{% for %}` (including `loop.*`), `{% set %}`, `{% raw %}`, comments, inline
  `{% include 'file' %}` and piping through 40+ filters.
- Template functions: `secret()`, `env()`, `file()`, `include()`, `glob()`, `exists()`,
  `which()`, `now()`, `today()` and `run()`, plus optional strict mode for undefined variables.

**Secrets**

- Backends: `env`, `file` (dotenv), `pass`, `gpg`, `age`, `op` (1Password), `bw` (Bitwarden),
  `keyring`, `sops` and arbitrary `command` backends, resolved in a configurable order with
  per-name caching and graceful fallbacks.
- `dotdeployer secrets list|check|encrypt` and `dotdeployer encrypt` with in-place encryption.

**Deployment**

- Transactional applies: every replaced target is stored in a timestamped backup set
  (files, symlinks and directories) with a manifest, and recorded in `.dotdeployer/state.json`
  together with fingerprints.
- Drift detection: files changed after deployment are reported instead of being silently
  overwritten; `--force` overwrites, `adopt` takes the local version back.
- Pruning of targets whose entries disappeared, `uninstall` of everything deployed, and
  `rollback` (by set id or `--last`, optionally restoring the previous state file).
- Atomic writes, umask-respecting modes, `chmod`/`chown` support, parent directory creation and
  a project lock that keeps concurrent applies apart.

**Hooks**

- 15 phases (`pre_init`, `post_init`, `pre_plan`, `post_plan`, `pre_apply`, `post_apply`,
  `pre_entry`, `post_entry`, `pre_remove`, `post_remove`, `pre_restore`, `post_restore`,
  `on_error`, `pre_doctor`, `post_doctor`) with templated commands, context variables, per-hook
  timeouts, `DOTDEPLOYER_*` environment, dry-run awareness and a manual `hook <phase>` command.

**CLI**

- Commands: `init`, `apply`, `plan`, `status`, `diff`, `doctor`, `list`, `info`, `tags`,
  `profiles`, `rollback`, `backup`, `uninstall`, `adopt`, `secrets`, `encrypt`, `hook`,
  `report`, `config`, `completion`, `version`, `help`.
- Dry-run (`-n`), non-interactive (`-y`, `--no-input`), machine-readable `--json`, quiet/verbose
  levels, colour control, shell completions for bash/zsh/fish and stable exit codes
  (0/1/2/3/4/5/6/7/8/9/10/11/12) for CI.
- Self-contained HTML reports and JSON reports (`dotdeployer report`, `--format`, `-o -`).

**Quality**

- 236 unit and integration tests using only `unittest`, a `doctor` command with 30+ checks, and
  a GitHub Actions workflow that runs tests on Python 3.9–3.13 across Linux, macOS and Windows.

[Unreleased]: https://github.com/hrfrutrd-dev/dotdeployer/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/hrfrutrd-dev/dotdeployer/releases/tag/v1.0.0

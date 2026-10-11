# dotdeployer

**Declarative, transactional dotfile deployment for people who are tired of `ln -s`.**

dotdeployer keeps your dotfiles in a git repository, describes where they belong in a single
manifest, and deploys them with backups, drift detection and rollback. It has **zero runtime
dependencies**, works on Linux, macOS and Windows, and never touches a file it cannot restore.

```console
$ dotdeployer plan --diff
  + ~/.zshrc                    → dotfiles/shell/zshrc
  ~ ~/.gitconfig                → dotfiles/git/gitconfig

── summary ────────────────────────────────────────────────────────────
result         1 create · 1 update
plan           1 create · 1 update

$ dotdeployer apply -y
✓ ~/.zshrc                    → dotfiles/shell/zshrc
✓ ~/.gitconfig                rendered from dotfiles/git/gitconfig.tmpl  [backup: 20261011-101552-4821]

── summary ────────────────────────────────────────────────────────────
result         2 applied
backups        20261011-101552-4821
duration       12 ms
hooks          2 run, 0 failed
✓ apply finished successfully
```

---

## Table of contents

- [Why](#why)
- [Install](#install)
- [Quick start](#quick-start)
- [The manifest](#the-manifest)
- [Deployment modes](#deployment-modes)
- [Conflict strategies](#conflict-strategies)
- [Profiles, tags and conditionals](#profiles-tags-and-conditionals)
- [Templating](#templating)
- [Secrets](#secrets)
- [Hooks](#hooks)
- [Day-to-day commands](#day-to-day-commands)
- [Backups and rollback](#backups-and-rollback)
- [Drift, doctor and CI](#drift-doctor-and-ci)
- [Reporting](#reporting)
- [Configuration reference](#configuration-reference)
- [Exit codes](#exit-codes)
- [Python API](#python-api)
- [Development](#development)
- [Design notes](#design-notes)

---

## Why

Symlinking a directory of dotfiles works until it doesn't:

| Problem | dotdeployer's answer |
| --- | --- |
| You edit a deployed file and forget which repo it came from | `status` shows drift per entry, `diff` shows the pending hunk |
| A deploy overwrites something you cared about | Every overwrite is backed up into a timestamped set, `rollback` restores it |
| Different machines need different values | Profiles, tags, host/platform conditions and template variables |
| Some files are templates, some are copies, some are symlinks | `mode = "symlink" \| "copy" \| "template" \| "mkdir" \| "ignore"` **per entry** |
| Secrets end up committed | `secret()` resolves at render time from env, files, `pass`, 1Password, … |
| You want to see what would happen before it happens | `plan`, `--dry-run`, `--json` for machines, deterministic exit codes for CI |

Everything is declared in one file:

```toml
[files]
"~/.zshrc"        = { source = "shell/zshrc", tags = ["shell"] }
"~/.gitconfig"    = { source = "git/gitconfig.tmpl", mode = "template", tags = ["git"] }
"~/.config/nvim"  = { source = "nvim", tags = ["nvim"], profiles = ["personal"] }
```

---

## Install

dotdeployer is a pure-Python package (3.9+) with **no runtime dependencies**.

```console
# recommended: isolated install with pipx / uv
pipx install git+https://github.com/hrfrutrd-dev/dotdeployer.git

# or with pip
pip install git+https://github.com/hrfrutrd-dev/dotdeployer.git

# or run straight from a checkout, no install at all
git clone https://github.com/hrfrutrd-dev/dotdeployer
cd dotdeployer && PYTHONPATH=src python3 -m dotdeployer --help
```

Both `dotdeployer` and the short alias `dd` are installed.

Python 3.11+ reads TOML from the standard library; on 3.9/3.10 install the extra
`pip install ".[toml]"` or simply use JSON for your manifest.

---

## Quick start

```console
$ dotdeployer init ~/dotfiles       # scaffold config, sample entry and hook
$ cd ~/dotfiles
$ mv ~/.zshrc dotfiles/shell/zshrc  # move your real files into the repo
$ dotdeployer adopt ~/.vimrc        # …or let dotdeployer move them for you
$ dotdeployer plan --diff           # look before you leap
$ dotdeployer apply                 # deploy, with backups
```

`adopt` moves an existing file into the repository, rewrites the manifest, and deploys it back —
the fastest way to bring an existing `$HOME` under management.

---

## The manifest

A project is a directory (usually a git repo) containing a manifest named
`dotdeployer.toml`, `.dotdeployer.toml`, `dotdeployer.json` or `dotdeployer.yaml`.
dotdeployer searches the current directory and then its parents, so any command works from a
subdirectory.

```toml
# ── global defaults ────────────────────────────────────────────────────────────
[settings]
source_root  = "dotfiles"   # where sources live, relative to the project root
strategy     = "backup"     # default conflict strategy
backup_keep  = 10           # keep the N most recent backup sets
prune        = true         # remove targets whose entry disappeared
strict_templates = false    # error on undefined template variables

# ── variables available to templates ───────────────────────────────────────────
[vars]
editor = "nvim"

# ── profiles: named bundles of variables and extra entries ─────────────────────
[profiles.work]
description = "work laptop"
vars        = { git_email = "me@company.com" }
entries     = ["~/.config/work"]      # extra entries only for this profile
tags        = ["work"]

[profiles.personal]
vars = { git_email = "me@example.com" }

# ── entries ────────────────────────────────────────────────────────────────────
[files]
"~/.zshrc"       = { source = "shell/zshrc", tags = ["shell"] }
"~/.gitconfig"   = { source = "git/gitconfig.tmpl", mode = "template",
                     tags = ["git"], profiles = ["work", "personal"] }
"~/.ssh/config"  = { source = "ssh/config", mode = "template",
                     permissions = "600", strategy = "override", tags = ["ssh"] }

"~/.config/nvim" = { source = "nvim", tags = ["nvim"], platforms = ["linux", "darwin"] }
"~/.wezterm.lua" = { source = "wezterm/init.lua", when = "os == 'darwin'" }
"~/.vim"         = { source = "vim", disabled = true }   # kept but never deployed

[dirs]
"~/.local/bin"   = {}
"~/.cache/tmp"   = { permissions = "700" }
```

Shorthand tables (`files`, `symlinks`, `copies`, `templates`, `dirs`, `directories`) select the
mode; a bare string is a path inside `source_root`:

```toml
[files]
"~/.zshrc" = "shell/zshrc"                                   # same as the table form
```

An entry accepts:

| Key | Meaning |
| --- | --- |
| `source` / `src` | Path inside `source_root` (or absolute, or `~/…`) |
| `target` / `dest` | Where it belongs; the table key is the usual way to write it |
| `mode` | `symlink` (default), `copy`, `template`, `mkdir`, `ignore` |
| `strategy` / `on_conflict` | `backup` (default), `override`, `skip`, `ask`, `merge` |
| `permissions` | Octal string or integer applied to the deployed path |
| `owner` / `group` | Applied when running with the privileges to do so |
| `when` / `unless` | Condition, see [conditionals](#profiles-tags-and-conditionals) |
| `tags` | Free-form labels used for selection |
| `profiles` | Only deploy under these profiles |
| `platforms` / `hosts` | Shorthand conditions on OS / hostname |
| `vars` | Per-entry template variables |
| `encrypt` / `decrypt` | Encrypt the source in place, or decrypt it while deploying |
| `secrets` | Names passed to `secret()` for the entry |
| `parents` / `create_parents` | Create missing parent directories (default `true`) |
| `strip_trailing_whitespace`, `ensure_newline` | Text normalisation for `copy`/`template` |
| `expand_env` | Expand `$VAR` in the source/target path (default `true`) |
| `disabled` / `enabled` | Temporarily take an entry out of rotation |

---

## Deployment modes

| Mode | What happens | Typical use |
| --- | --- | --- |
| `symlink` | Target becomes a symlink to the source | Version-controlled configs you keep editing |
| `copy` | Source is copied over the target | Files an application rewrites, Windows without symlink rights |
| `template` | Source is rendered with the template engine and written | Machine-specific values, secrets, conditionals |
| `mkdir` | The directory is created (mode/permissions applied) | `~/.local/bin`, plugin caches |
| `ignore` | Matched, validated and reported, but never deployed | Documenting a path, or pausing an entry |

Symlinks are still the default because they make edits instantly visible in git; switch an entry
to `copy` when the tool that reads it cannot follow links.

---

## Conflict strategies

When a target already exists and is **not** managed by dotdeployer, the strategy decides:

| Strategy | Behaviour |
| --- | --- |
| `backup` | Move the existing file into a backup set, then deploy |
| `override` | Replace it, no questions asked |
| `skip` | Leave the existing file alone, report `conflict` |
| `ask` | Prompt (interactive terminals only; `--yes` skips) |
| `merge` | Merge directories file-by-file instead of replacing them |

Whatever the strategy, a file that dotdeployer deployed before and that you edited since is
**drift**, not a conflict: `apply` refuses to clobber it unless you pass `--force`, and
`--adopt` instead pulls the local version back into the repository.

---

## Profiles, tags and conditionals

Everything is selected on the command line — nothing about a profile is baked in:

```console
$ dotdeployer apply -p work                  # activate a profile
$ dotdeployer apply --tag git,nvim           # only tagged entries
$ dotdeployer apply --exclude-tag wip
$ dotdeployer apply --only home-.zshrc       # one entry (short name or target)
$ dotdeployer apply --skip home-.bashrc
```

`-p` may be repeated (`-p work -p laptop`), and `DOTDEPLOYER_PROFILE=work` sets the default.
Profiles add variables and can inject extra entries via `include`.

Conditions are small expressions evaluated against facts and variables:

```toml
"~/.wezterm.lua" = { source = "wezterm.lua", when = "os == 'darwin'" }
"~/.p10k.zsh"    = { source = "p10k.zsh", when = "hostname matches 'mac-*'" }
"~/.tmux.conf"   = { source = "tmux.conf", unless = "os == 'windows'" }
"~/.config/work" = { source = "work", when = "env == 'ci'" }
```

Supported: `==`, `!=`, `<`, `<=`, `>`, `>=`, `and`, `or`, `not`, `in`, `not in`,
`matches`/`fnmatch`, and plain truthiness. Facts: `os`, `family`, `arch`, `kernel`, `hostname`,
`user`, `home`, `distro`, `distro_version`, `distro_like`, `python`, `cpu_count`, `memory_gb`,
`shell`, `is_wsl`, `is_container`, `is_root`, `has_sudo` — available both bare and under the
`facts.` namespace. Helper functions such as `has('git')`, `which('nvim')`, `env('CI')` and
`exists('~/.ssh')` are callable inside conditions. Lists of conditions are ANDed.

---

## Templating

`mode = "template"` renders the source with a small, dependency-free engine that borrows the
syntax you already know from Jinja2:

```jinja
{# ~/.gitconfig.tmpl #}
[user]
    name  = {{ git_name }}
    email = {{ git_email | default("unknown") }}

[core]
    editor = {{ editor | upper }}

{% if os == "darwin" %}
[credential]
    helper = osxkeychain
{% elif os == "linux" %}
[credential]
    helper = {{ credential_helper | default("cache --timeout=3600") }}
{% endif %}

{% for host in extra_hosts %}
[url "git@{{ host }}:"]
    insteadOf = https://{{ host }}/
{% endfor %}
```

- `{{ expression }}` — interpolation with filters (`upper`, `lower`, `join`, `replace`, `sort`,
  `default`, `length`, `human_size`, …)
- `{% if %} {% elif %} {% else %} {% endif %}`, `{% for x in xs %} … {% else %} … {% endfor %}`
- `{% set name = value %}`, `{# comments #}`, `{% raw %}…{% endraw %}`, `{% include 'file' %}`
- Loops expose `loop.index`, `loop.index0`, `loop.first`, `loop.last`, `loop.length`
- Functions: `secret(name[, default])`, `env(name[, default])`, `file(path)`,
  `include('path')`, `exists(path)`, `which(program)`, `glob(pattern)`, `now()`/`today(fmt)`,
  and `run(command[, fallback, timeout])` which executes a command while rendering
- Undefined variables render as an empty string unless `strict_templates = true`, so
  `{{ value | default("fallback") }}` always works

The same engine renders hook commands and manifest values, which is how `git_email` above can
differ per profile without duplicating entries.

---

## Secrets

`{{ secret("github_token") }}` asks every configured backend in order until one answers:

```toml
[secrets]
order = ["env", "file", "pass", "op"]      # default: env, file, pass, gpg, age, op, bw, keyring, sops, command
file  = "~/.secrets.env"                   # or: file = "secrets/tokens.env"
pass_prefix = "dotfiles/"
vault = "Private"
```

| Backend | Reads from |
| --- | --- |
| `env` | `$NAME` (or a custom prefix such as `DOTFILES_`) |
| `file` | A `.env`-style file: `NAME=value` lines, `#` comments |
| `pass` | `pass show <prefix>/<name>` |
| `gpg` / `age` | Encrypted files under `secrets/` |
| `op` | 1Password CLI: `op read "op://vault/item/field"` |
| `bw` | Bitwarden CLI |
| `keyring` | The system keyring (`pip install keyring`) |
| `sops` | `sops -d <file>` |
| `command` | Any command whose stdout is the value |

Inspect what is available and which secrets your entries reference:

```console
$ dotdeployer secrets list      # active backends plus every referenced secret
$ dotdeployer secrets check     # resolve every referenced secret, report what is missing
$ dotdeployer secrets encrypt secrets/api_token   # encrypt a source file in place
$ dotdeployer encrypt secrets/api_token
```

Missing secrets never break the plan: `plan` renders the fallback (or an empty string) and
`doctor` reports a warning so you can see it before `apply`.

---

## Hooks

```toml
[hooks]
pre_apply   = ["echo deploying to {{ hostname }} ({{ profiles | join(', ') }})"]
post_entry  = ["./hooks/reload-shell.sh"]
post_apply  = ["git -C {{ root }} status --short"]
on_error    = ["echo 'apply failed: {{ error }}' >&2"]
```

Phases: `pre_init`, `post_init`, `pre_plan`, `post_plan`, `pre_apply`, `post_apply`,
`pre_entry`, `post_entry`, `pre_remove`, `post_remove`, `pre_restore`, `post_restore`,
`pre_doctor`, `post_doctor`, `on_error`.

`pre_init`/`post_init` run when `init` scaffolds a project (the hooks of the manifest it just
wrote), `pre_plan`/`post_plan` around every `plan`/`status`/`diff`, `pre_doctor`/`post_doctor`
around `doctor`, and the rest around `apply`, `uninstall` and `rollback`.

- Commands are templated with the same variables as templates plus phase-specific ones
  (`entry`, `target`, `status`, `error`, `applied`, …).
- Hooks receive `DOTDEPLOYER_PHASE`, `DOTDEPLOYER_ROOT`, `DOTDEPLOYER_CONFIG`,
  `DOTDEPLOYER_HOSTNAME`, `DOTDEPLOYER_PROFILES` and `DOTDEPLOYER_<CONTEXT>` variables.
- `dotdeployer hook post_apply` runs a phase by hand; `--no-hooks` (or
  `DOTDEPLOYER_HOOKS=0`) disables them for one run.
- Failures stop the run unless `hooks_continue_on_error = true`; per-hook timeouts come from
  `hook_timeout`.

---

## Day-to-day commands

| Command | What it does |
| --- | --- |
| `init [DIR]` | Scaffold a project (`--force` overwrites, `--minimal` skips samples) |
| `plan [--diff]` | Show what `apply` would do, exit 0 |
| `status` | Same list, but exit 12 when work is pending (CI gate) |
| `diff` | Show the pending content diffs, exit 12 when work is pending |
| `apply` | Deploy, backing up anything it replaces |
| `doctor` | Validate config, templates, targets, secrets, drift |
| `list` | Entries with mode, tags and (optionally) status |
| `tags`, `profiles` | Discover what the manifest offers |
| `info` | Project, state and backup statistics |
| `backup ls / purge / restore` | Manage backup sets |
| `rollback [SET]` | Undo a set (`--last`, `--list`, `--keep-state`) |
| `uninstall` | Remove every deployed target (backups are kept) |
| `adopt PATH…` | Move existing files into the repo and deploy them |
| `secrets list / check / encrypt` | Work with secret backends |
| `hook PHASE` | Run one hook phase manually |
| `report` | Write an HTML/JSON report of the current state |
| `config path / show / validate` | Inspect the resolved configuration |
| `completion bash\|zsh\|fish` | Print a completion script |
| `version` | Print the version (`--json` for scripts) |

Flags shared by most commands:

```
-c, --config PATH   -p, --profile NAME    -V, --var KEY=VALUE    -n, --dry-run
-y, --yes           --no-input            --force               --no-hooks
-q, --quiet         -v, --verbose         --color auto|always|never   --json
```

Nothing is written under `-n/--dry-run`, and `--json` never pollutes stdout with anything
but the JSON document, so both compose cleanly in scripts.

---

## Backups and rollback

Every `apply` that replaces something writes the old contents into
`.dotdeployer/backups/<timestamp>-<pid>/` together with a `manifest.json` recording the entry,
target, kind and (for symlinks) the link target:

```console
$ dotdeployer backup ls
20261011-101552-4821   2 items   3.1KiB   12s ago
  001-home-.gitconfig  file  ~/.gitconfig
  002-home-.zshrc      file  ~/.zshrc

$ dotdeployer rollback --last -y     # put both files back, restore the state file too
$ dotdeployer backup purge --keep 5  # housekeeping
```

Sets are created lazily — only entries that actually replaced something are added, and an
empty set is discarded. Old sets are pruned to `backup_keep`.

---

## Drift, doctor and CI

```console
$ dotdeployer status --check ; echo $?
12
$ dotdeployer status --strict ; echo $?    # drift counts as a failure too
11
```

`dotdeployer doctor` is a full pre-flight check — 30+ assertions across the environment,
manifest, templates, targets, secrets, hooks and drift:

```console
$ dotdeployer doctor
✓ environment · python 3.11.2 on linux
✓ config · 8 entries from dotdeployer.toml
✓ state · 8 tracked entries, no orphans
! drift · ~/.zshrc was modified since deployment
! secret · 'missing_one' is not provided by any backend
…

── diagnosis ──────────────────────────────────────────────────────────
passed         21
warnings       2
failures       0
checks         34
```

A GitHub Actions workflow ships in `.github/workflows/ci.yml`; for your own dotfiles repo:

```yaml
- run: dotdeployer doctor --strict
- run: dotdeployer status --check        # fails when the machine drifted from the repo
```

---

## Reporting

```console
$ dotdeployer report                    # dotdeployer-report.html, self-contained
$ dotdeployer report --format json -o - # machine readable, straight to stdout
```

The HTML report embeds its own CSS (no CDN, no JavaScript) and lists the environment, the
summary counts, every entry with its status, mode, size, tags and diff, plus warnings and
doctor checks — handy as a CI artifact or a "what is on this machine" page.

---

## Configuration reference

### Files

| File | Purpose |
| --- | --- |
| `dotdeployer.toml` / `dotdeployer.json` (also the `.dotdeployer.*` spellings) | The manifest; searched upwards from the cwd |
| `dotdeployer.yaml` | Also detected, but needs PyYAML installed |
| `include = [...]` | Extra manifest files, merged underneath the main one |
| `hosts/<hostname>.toml` (or `host-<hostname>.toml`) | Per-machine overrides, merged when present |
| `local.toml` / `.dotdeployer.local.toml` | Machine-local overrides, normally git-ignored |
| `.dotdeployer/state.json` | Deployed entries, fingerprints and history (git-ignore it) |
| `.dotdeployer/backups/` | Backup sets (git-ignore it) |
| `.dotdeployer/state.json.lock` | Lock file that keeps concurrent applies apart |

Merge order, lowest priority first: `include` → manifest → host file → local file → profile →
`-V/--var` on the command line.

### Settings

| Key | Default | Meaning |
| --- | --- | --- |
| `source_root` | `"dotfiles"` | Where sources live relative to the project |
| `strategy` | `"backup"` | Default conflict strategy |
| `backup_keep` | `10` | Backup sets to keep |
| `prune` | `true` | Remove targets whose entry disappeared |
| `lock_timeout` | `30` | Seconds to wait for the project lock |
| `strict_templates` | `false` | Fail on undefined variables |
| `fail_on_drift` | `false` | Treat drift as an error |
| `hooks_enabled` | `true` | Master switch for hooks |
| `hook_shell` | `"/bin/sh"` | Shell used for hook commands |
| `hook_timeout` | `300` | Per-hook timeout in seconds |
| `max_backup_bytes` | `268435456` | Refuse to back up anything larger |
| `expand_env` | `true` | Expand `$VAR`/`~` in paths |
| `diff_context` | `3` | Context lines in diffs |

---

## Exit codes

Exit codes are stable so CI can tell "nothing to do" from "something broke":

| Code | Meaning |
| --- | --- |
| `0` | Success / nothing to do |
| `1` | Unclassified error |
| `2` | Configuration or usage error |
| `3` | Template error |
| `4` | Secret error |
| `5` | Action error (I/O, permissions) |
| `6` | Hook failure |
| `7` | Selection error (unknown tag/entry/profile) |
| `8` | State/lock error |
| `9` | Lock held by another process |
| `10` | Conflicts need a decision (`status`, `plan --check`, `apply --check`) |
| `11` | Drift detected (`--strict`) |
| `12` | Changes are pending (`status --check`) |

---

## Python API

dotdeployer is importable as a library, which makes it easy to extend or embed:

```python
from dotdeployer import Project

project = Project.load("~/dotfiles", profiles=["work"], dry_run=True)
plan = project.plan(with_diffs=True)

for change in plan.changes:
    print(change.status, change.entry.target)

project.apply(plan)          # transactional, backs up and records state
project.rollback(last=True)  # or project.uninstall(), project.doctor(), project.adopt([...])
```

---

## Development

```console
git clone https://github.com/hrfrutrd-dev/dotdeployer && cd dotdeployer
python3 -m unittest discover -s tests -t .      # 236 tests, standard library only
ruff check src tests                            # lint (optional)
```

The test suite never touches your real `$HOME`: every test builds a temporary project and home
directory. Contributions are welcome — please keep new behaviour covered by a test and keep the
package dependency-free.

---

## Design notes

- **One manifest, no magic directories.** Every managed path is written down explicitly, so
  `git grep ~/.zshrc` answers "what owns this file?".
- **State is a cache, not the truth.** the manifest is authoritative; `.dotdeployer/state.json`
  only remembers what was deployed, when, and with which fingerprint — delete it and `apply`
  rebuilds it without touching your files.
- **Transactional by default.** Back up, then write, then record. If a write fails mid-run the
  remaining entries still get a chance (unless `--fail-fast`), and `rollback` can undo the set.
- **Preview everything.** `plan` and `--dry-run` run the same code path as `apply`, so what you
  see is what you get — including rendered templates and resolved secrets.
- **Zero dependencies.** A dotfile tool is something you run on a fresh machine with nothing
  installed; it only ever needs Python itself.

## License

MIT — see [LICENSE](LICENSE).

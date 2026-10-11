"""Command line interface.

Run ``dotdeployer --help`` for the full command list.  Every command supports
``--json`` for scripting; mutation commands exit with meaningful codes:

===== =========================================
code  meaning
===== =========================================
0     success / already up to date
1     runtime error
2     configuration error
3     template error
4     secret error
5     file system action failed
6     hook failed
7     selection error
8     state error
9     another run holds the lock
10    conflicts detected (``plan``)
11    local drift detected (``plan``)
12    changes pending (``plan --check``)
===== =========================================
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import __version__
from .doctor import render as render_checks
from .doctor import summarise, worst_status
from .errors import (
    ActionError,
    ConfigError,
    DotDeployerError,
    SelectionError,
)
from .hooks import PHASES, HookRunner
from .project import ApplyResult, Project
from .report import write_report
from .secrets import encrypt_bytes
from .ui import Console, Style
from .util import display_path, human_size, truncate

PROG = "dotdeployer"
COMMANDS = [
    "init", "apply", "plan", "status", "diff", "doctor", "list", "info", "tags", "profiles",
    "rollback", "backup", "uninstall", "adopt", "secrets", "encrypt", "hook", "report",
    "config", "completion", "version", "help",
]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFLICT = 10
EXIT_DRIFT = 11
EXIT_PENDING = 12


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def build_console(args: argparse.Namespace) -> Console:
    color_setting = getattr(args, "color", "auto")
    color: Optional[bool] = None
    if color_setting == "always":
        color = True
    elif color_setting == "never":
        color = False
    verbosity = 1 + int(getattr(args, "verbose", 0))
    verbosity = max(0, min(3, verbosity))
    quiet = bool(getattr(args, "quiet", 0)) or bool(getattr(args, "json", False))
    return Console(color=color, quiet=quiet, verbosity=verbosity)


def emit_json(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, ensure_ascii=False, default=str)
    sys.stdout.write("\n")


def parse_var(raw: str) -> tuple:
    if "=" not in raw:
        raise ConfigError(f"--var expects KEY=VALUE, got {raw!r}")
    key, _, value = raw.partition("=")
    key = key.strip()
    if not key:
        raise ConfigError("--var needs a non-empty key")
    lowered = value.strip().lower()
    if lowered in {"true", "false"}:
        parsed: Any = lowered == "true"
    elif lowered in {"null", "none"}:
        parsed = None
    else:
        try:
            parsed = int(value)
        except ValueError:
            try:
                parsed = float(value)
            except ValueError:
                parsed = value
    return key, parsed


def collect_vars(args: argparse.Namespace) -> Dict[str, Any]:
    variables: Dict[str, Any] = {}
    for raw in getattr(args, "var", None) or []:
        key, value = parse_var(raw)
        variables[key] = value
    return variables


def load_project(args: argparse.Namespace, console: Console, *, dry_run: Optional[bool] = None) -> Project:
    config_path = getattr(args, "config", None) or os.environ.get("DOTDEPLOYER_CONFIG")
    profiles = list(getattr(args, "profile", None) or [])
    if os.environ.get("DOTDEPLOYER_PROFILE") and not profiles:
        profiles = [p for p in os.environ["DOTDEPLOYER_PROFILE"].split(",") if p]
    return Project.load(
        config_path,
        profiles=profiles,
        variables=collect_vars(args),
        console=console,
        dry_run=bool(getattr(args, "dry_run", False)) if dry_run is None else dry_run,
        force=bool(getattr(args, "force", False)),
        assume_yes=bool(getattr(args, "yes", False)),
        no_input=bool(getattr(args, "no_input", False)),
        hooks_enabled=False if getattr(args, "no_hooks", False) else None,
    )


def selection_kwargs(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "tags": list(getattr(args, "tag", None) or []),
        "exclude_tags": list(getattr(args, "exclude_tag", None) or []),
        "only": list(getattr(args, "only", None) or []),
        "skip": list(getattr(args, "skip", None) or []),
    }


def confirm_destructive(args: argparse.Namespace, console: Console, message: str) -> bool:
    from .ui import prompt_yes_no

    return prompt_yes_no(
        message,
        default=False,
        console=console,
        assume_yes=bool(getattr(args, "yes", False)),
        no_input=bool(getattr(args, "no_input", False)),
    )


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Declarative, transactional dotfile deployment.",
        epilog=textwrap.dedent(
            """\
            examples:
              dotdeployer init ~/dotfiles          scaffold a new project
              dotdeployer plan --diff              show what would change, with diffs
              dotdeployer apply -p work            deploy the 'work' profile
              dotdeployer apply --tag nvim -y      deploy only git/nvim tagged entries
              dotdeployer status --check           exit 12 when changes are pending (CI)
              dotdeployer rollback --list          inspect backups
              dotdeployer doctor                   validate config, templates and targets
              dotdeployer report -o report.html    generate a shareable HTML report
            """
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", metavar="PATH",
                        help="path to the project root or config file (default: search upwards)")
    common.add_argument("-p", "--profile", action="append", metavar="NAME",
                        help="activate a profile (repeatable, comma separated works too)")
    common.add_argument("-V", "--var", action="append", metavar="KEY=VALUE",
                        help="override a template/config variable (repeatable)")
    common.add_argument("-n", "--dry-run", action="store_true", help="show what would happen, change nothing")
    common.add_argument("-y", "--yes", action="store_true", help="assume yes for all prompts")
    common.add_argument("--no-input", action="store_true", help="never prompt; use defaults")
    common.add_argument("--force", action="store_true",
                        help="overwrite targets that drifted or are managed by someone else")
    common.add_argument("--no-hooks", action="store_true", help="skip all hook commands")
    common.add_argument("-q", "--quiet", action="count", default=0, help="less output (-qq for errors only)")
    common.add_argument("-v", "--verbose", action="count", default=0, help="more output (-vv for debug)")
    common.add_argument("--color", choices=["auto", "always", "never"], default="auto",
                        help="colourise output (default: auto)")
    common.add_argument("--json", action="store_true", help="machine readable output")
    common.add_argument("--no-color", dest="color", action="store_const", const="never",
                        help="alias for --color never")

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # -- init ---------------------------------------------------------------
    p_init = sub.add_parser("init", parents=[common], help="scaffold a new dotdeployer project")
    p_init.add_argument("directory", nargs="?", default=".", help="where to create the project")
    p_init.add_argument("--name", help="project name (defaults to the directory name)")
    p_init.add_argument("--minimal", action="store_true", help="skip the example dotfiles")
    p_init.set_defaults(func=cmd_init)

    # -- apply --------------------------------------------------------------
    p_apply = sub.add_parser("apply", parents=[common], help="deploy entries and update the state file")
    p_apply.add_argument("--tag", action="append", metavar="TAG", help="only entries with this tag")
    p_apply.add_argument("--exclude-tag", action="append", metavar="TAG", help="skip entries with this tag")
    p_apply.add_argument("--only", action="append", metavar="NAME", help="only this entry (name or tag)")
    p_apply.add_argument("--skip", action="append", metavar="NAME", help="skip this entry")
    p_apply.add_argument("--no-prune", action="store_true", help="keep files that are no longer managed")
    p_apply.add_argument("--prune", action="store_true", help="remove files that are no longer managed")
    p_apply.add_argument("--no-backup", action="store_true", help="do not back up replaced files")
    p_apply.add_argument("--fail-fast", action="store_true", help="stop at the first failing entry")
    p_apply.add_argument("--confirm", action="store_true", help="ask before applying")
    p_apply.add_argument("--check", action="store_true",
                         help="exit 10/11/12 when conflicts/drift/changes are found, change nothing")
    p_apply.add_argument("--keep-backups", type=int, metavar="N", help="retain only N backup sets")
    p_apply.set_defaults(func=cmd_apply)

    # -- plan / status / diff ----------------------------------------------
    for name, help_text in (("plan", "show what apply would do"),
                            ("status", "report pending changes (exit 12 when work is pending)"),
                            ("diff", "show pending content diffs (exit 12 when work is pending)")):
        p_plan = sub.add_parser(name, parents=[common], help=help_text)
        p_plan.add_argument("--tag", action="append", metavar="TAG")
        p_plan.add_argument("--exclude-tag", action="append", metavar="TAG")
        p_plan.add_argument("--only", action="append", metavar="NAME")
        p_plan.add_argument("--skip", action="append", metavar="NAME")
        p_plan.add_argument("--diff", dest="show_diff", action="store_true",
                            help="show content diffs for pending changes")
        p_plan.add_argument("--all", dest="show_all", action="store_true", help="include unchanged entries")
        p_plan.add_argument("--limit", type=int, help="show at most N entries")
        p_plan.add_argument("--no-diff", action="store_true", help="do not compute diffs (faster)")
        p_plan.add_argument("--check", action="store_true",
                            help="exit non-zero when action is required (CI friendly)")
        p_plan.add_argument("--strict", action="store_true", help="treat drift and conflicts as failures")
        # `plan` reports and always exits 0; `status`/`diff` double as CI gates
        p_plan.set_defaults(func=cmd_plan, show_diff=(name == "diff"), check=(name != "plan"))

    # -- doctor -------------------------------------------------------------
    p_doctor = sub.add_parser("doctor", parents=[common], aliases=["verify", "check"],
                              help="validate configuration, templates, targets and secrets")
    p_doctor.add_argument("--strict", action="store_true", help="exit non-zero on warnings as well")
    p_doctor.add_argument("--show-info", action="store_true", help="also list informational checks")
    p_doctor.add_argument("--no-drift", action="store_true", help="skip drift/conflict detection")
    p_doctor.set_defaults(func=cmd_doctor)

    # -- list ---------------------------------------------------------------
    p_list = sub.add_parser("list", parents=[common], aliases=["ls", "entries"],
                            help="list managed entries")
    p_list.add_argument("--tag", action="append", metavar="TAG")
    p_list.add_argument("--long", action="store_true", help="include resolved paths and metadata")
    p_list.add_argument("--status", action="store_true", help="include the current status of each entry")
    p_list.set_defaults(func=cmd_list)

    # -- info ---------------------------------------------------------------
    p_info = sub.add_parser("info", parents=[common], help="show project, state and backup statistics")
    p_info.set_defaults(func=cmd_info)

    # -- tags / profiles ----------------------------------------------------
    p_tags = sub.add_parser("tags", parents=[common], help="list tags and their entries")
    p_tags.set_defaults(func=cmd_tags)
    p_profiles = sub.add_parser("profiles", parents=[common], help="list profiles")
    p_profiles.add_argument("--activate", metavar="NAME", help="show which entries a profile would enable")
    p_profiles.set_defaults(func=cmd_profiles)

    # -- rollback -----------------------------------------------------------
    p_roll = sub.add_parser("rollback", parents=[common], help="restore a backup set")
    p_roll.add_argument("backup_id", nargs="?", help="backup set id (default: newest)")
    p_roll.add_argument("--list", dest="list_only", action="store_true", help="list backup sets")
    p_roll.add_argument("--last", action="store_true", help="restore the newest set (the default)")
    p_roll.add_argument("--keep-state", action="store_true", help="keep state entries for restored files")
    p_roll.set_defaults(func=cmd_rollback)

    # -- backup -------------------------------------------------------------
    p_backup = sub.add_parser("backup", parents=[common], help="inspect and prune backups")
    backup_sub = p_backup.add_subparsers(dest="backup_command", metavar="ACTION")
    b_ls = backup_sub.add_parser("ls", parents=[common], help="list backup sets")
    b_ls.set_defaults(func=cmd_backup_ls)
    b_purge = backup_sub.add_parser("purge", parents=[common], help="delete old backup sets")
    b_purge.add_argument("--keep", type=int, default=None, help="number of sets to keep (default: setting)")
    b_purge.set_defaults(func=cmd_backup_purge)
    b_restore = backup_sub.add_parser("restore", parents=[common], help="restore a set (alias of rollback)")
    b_restore.add_argument("backup_id", nargs="?", help="backup set id (default: newest)")
    b_restore.add_argument("--keep-state", action="store_true")
    b_restore.set_defaults(func=cmd_rollback)
    p_backup.set_defaults(func=lambda args, console: cmd_backup_ls(args, console))

    # -- uninstall ----------------------------------------------------------
    p_unin = sub.add_parser("uninstall", parents=[common], aliases=["remove"],
                            help="remove every deployed target")
    p_unin.add_argument("--only", action="append", metavar="NAME", help="remove just this entry")
    p_unin.add_argument("--keep-backups", action="store_true", help="do not back up removed files")
    p_unin.add_argument("--keep-state", action="store_true", help="keep the state entries")
    p_unin.set_defaults(func=cmd_uninstall)

    # -- adopt --------------------------------------------------------------
    p_adopt = sub.add_parser("adopt", parents=[common], help="move existing files into the project")
    p_adopt.add_argument("targets", nargs="+", help="files or directories inside $HOME")
    p_adopt.add_argument("--copy", action="store_true", help="copy instead of moving")
    p_adopt.add_argument("--mode", default="symlink", choices=["symlink", "copy", "template"],
                         help="deployment mode for the new entries")
    p_adopt.add_argument("--tag", action="append", metavar="TAG", help="tag the new entries")
    p_adopt.set_defaults(func=cmd_adopt)

    # -- secrets ------------------------------------------------------------
    p_secrets = sub.add_parser("secrets", parents=[common], aliases=["secret"],
                               help="inspect secret backends and referenced secrets")
    secrets_sub = p_secrets.add_subparsers(dest="secrets_command", metavar="ACTION")
    s_ls = secrets_sub.add_parser("list", parents=[common], aliases=["ls"], help="list referenced secrets")
    s_ls.add_argument("--values", action="store_true", help="show a masked preview of resolved values")
    s_ls.set_defaults(func=cmd_secrets_list)
    s_check = secrets_sub.add_parser("check", parents=[common], help="verify that every secret resolves")
    s_check.set_defaults(func=cmd_secrets_check)
    s_enc = secrets_sub.add_parser("encrypt", parents=[common], help="encrypt a file for the configured recipient")
    s_enc.add_argument("file", help="file to encrypt")
    s_enc.add_argument("-o", "--output", help="output path (default: <file>.gpg / .age)")
    s_enc.set_defaults(func=cmd_encrypt)
    p_secrets.set_defaults(func=cmd_secrets_list)

    # -- encrypt ------------------------------------------------------------
    p_enc = sub.add_parser("encrypt", parents=[common], help="encrypt a dotfile source in place")
    p_enc.add_argument("file", help="file to encrypt")
    p_enc.add_argument("-o", "--output", help="output path")
    p_enc.add_argument("--in-place", action="store_true", help="replace the input file")
    p_enc.set_defaults(func=cmd_encrypt)

    # -- hook ---------------------------------------------------------------
    p_hook = sub.add_parser("hook", parents=[common], help="run the hooks of a phase manually")
    p_hook.add_argument("phase", choices=PHASES, help="hook phase to run")
    p_hook.add_argument("--entry", help="context entry name for pre_entry/post_entry hooks")
    p_hook.set_defaults(func=cmd_hook)

    # -- report -------------------------------------------------------------
    p_report = sub.add_parser("report", parents=[common], help="generate an HTML or JSON report")
    p_report.add_argument("-o", "--output", help="output file (default: dotdeployer-report.html)")
    p_report.add_argument("--format", choices=["html", "json"], default=None, help="output format")
    p_report.add_argument("--no-diff", action="store_true", help="omit diffs from the report")
    p_report.set_defaults(func=cmd_report)

    # -- config -------------------------------------------------------------
    p_config = sub.add_parser("config", parents=[common], help="inspect the resolved configuration")
    config_sub = p_config.add_subparsers(dest="config_command", metavar="ACTION")
    c_path = config_sub.add_parser("path", parents=[common], help="print the config file path")
    c_path.set_defaults(func=cmd_config_path)
    c_show = config_sub.add_parser("show", parents=[common], help="print the merged configuration")
    c_show.add_argument("--resolved", action="store_true", help="include resolved targets and sources")
    c_show.set_defaults(func=cmd_config_show)
    c_val = config_sub.add_parser("validate", parents=[common], help="validate the configuration")
    c_val.set_defaults(func=cmd_config_validate)
    p_config.set_defaults(func=cmd_config_show)

    # -- completion ---------------------------------------------------------
    p_comp = sub.add_parser("completion", parents=[common], help="print a shell completion script")
    p_comp.add_argument("shell", nargs="?", default="bash", choices=["bash", "zsh", "fish"])
    p_comp.set_defaults(func=cmd_completion)

    # -- version / help -----------------------------------------------------
    p_version = sub.add_parser("version", parents=[common], help="print version information")
    p_version.set_defaults(func=cmd_version)
    p_help = sub.add_parser("help", parents=[common], help="show help for a command")
    p_help.add_argument("topic", nargs="?", help="command to describe")
    p_help.set_defaults(func=cmd_help, _parser=parser)

    return parser


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_init(args: argparse.Namespace, console: Console) -> int:
    target = Path(args.directory).expanduser().resolve()
    name = args.name or target.name
    config_path = target / "dotdeployer.toml"
    if config_path.exists() and not args.force:
        raise ConfigError(
            f"{display_path(config_path)} already exists",
            hint="use --force to overwrite it",
        )
    target.mkdir(parents=True, exist_ok=True)
    dotfiles = target / "dotfiles"
    hooks_dir = target / "hooks"
    dotfiles.mkdir(exist_ok=True)
    hooks_dir.mkdir(exist_ok=True)
    (target / ".dotdeployer").mkdir(exist_ok=True)

    if not args.minimal:
        (dotfiles / "git").mkdir(exist_ok=True)
        (dotfiles / "shell").mkdir(exist_ok=True)
        (dotfiles / "nvim").mkdir(exist_ok=True)
        _write_example_files(dotfiles, hooks_dir, name)

    config_path.write_text(_template_config(name, minimal=args.minimal), encoding="utf-8")
    gitignore = target / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(
            "# dotdeployer state and backups\n.dotdeployer/\n*.local.toml\n", encoding="utf-8"
        )

    if not args.no_hooks:
        _run_init_hooks(config_path, target, console)

    console.write("")
    console.ok(f"created project {console.paint(str(target), Style.BOLD)}")
    console.kv("config", display_path(config_path))
    console.kv("sources", display_path(dotfiles))
    console.kv("next", f"cd {target} && dotdeployer doctor && dotdeployer plan")
    return EXIT_OK


def _run_init_hooks(config_path: Path, target: Path, console: Console) -> None:
    """Run the freshly scaffolded project's ``pre_init``/``post_init`` hooks."""

    try:
        project = Project.load(
            str(config_path),
            console=console,
            dry_run=False,
            assume_yes=True,
            no_input=True,
        )
    except DotDeployerError as exc:  # a broken manifest must not break init
        console.debug(f"skipping init hooks: {exc}")
        return
    project.hooks.run("pre_init", {"target": str(target)}, check=False)
    project.hooks.run("post_init", {"target": str(target)}, check=False)


def _write_example_files(dotfiles: Path, hooks_dir: Path, name: str) -> None:
    (dotfiles / "shell" / "zshenv").write_text(
        "# managed by dotdeployer (symlinked to ~/.zshenv)\n"
        "export PAGER=less\nexport PATH=\"$HOME/.local/bin:$PATH\"\n",
        encoding="utf-8",
    )
    (dotfiles / "shell" / "zshrc").write_text(
        "# managed by dotdeployer - edit dotfiles/shell/zshrc, not ~/.zshrc\n"
        "{% if os == 'darwin' %}\neval \"$(brew shellenv)\"\n{% endif %}\n"
        "alias ll='ls -lah'\nalias gs='git status -sb'\n"
        "{% for alias_name, command in {'gco': 'git checkout', 'gp': 'git push'} %}\n"
        "alias {{ alias_name }}='{{ command }}'\n"
        "{% endfor %}\n",
        encoding="utf-8",
    )
    (dotfiles / "git" / "gitconfig").write_text(
        "[user]\n\tname = {{ git_name | default('Your Name') }}\n"
        "\temail = {{ git_email | default('you@example.com') }}\n"
        "[init]\n\tdefaultBranch = main\n"
        "[core]\n\teditor = {{ editor | default('nvim') }}\n"
        "[alias]\n\tst = status -sb\n\tco = checkout\n\tlg = log --oneline --graph --decorate\n",
        encoding="utf-8",
    )
    (dotfiles / "nvim" / "init.lua").write_text(
        "-- managed by dotdeployer\nvim.opt.number = true\nvim.opt.relativenumber = true\n"
        "vim.opt.expandtab = true\nvim.opt.shiftwidth = 2\nvim.opt.signcolumn = 'yes'\n"
        "vim.keymap.set('n', '<leader>w', ':w<CR>')\n",
        encoding="utf-8",
    )
    (hooks_dir / "reload-shell.sh").write_text(
        "#!/bin/sh\n"
        "# Example post_apply hook: sourced by the next shell instead of restarting it.\n"
        "echo \"dotdeployer: shell configuration updated\"\n",
        encoding="utf-8",
    )
    os.chmod(hooks_dir / "reload-shell.sh", 0o755)
    (dotfiles / "ssh_config").write_text(
        "# rendered from dotfiles/ssh_config\n"
        "Host *\n  ServerAliveInterval 60\n  AddKeysToAgent yes\n"
        "{% if os == 'darwin' %}\n  UseKeychain yes\n{% endif %}\n",
        encoding="utf-8",
    )


def _template_config(name: str, *, minimal: bool = False) -> str:
    if minimal:
        return f'''# dotdeployer project: {name}
# Docs: https://github.com/hrfrutrd-dev/dotdeployer
version = 1

[settings]
source_root = "dotfiles"   # where the real dotfiles live
strategy    = "backup"     # backup | override | skip | ask | merge
prune       = true         # remove files that are no longer managed

[vars]
editor    = "nvim"
git_email = ""

[profiles.personal]
description = "personal machines"
vars        = {{ git_email = "you@example.com" }}

# --- entries ------------------------------------------------------------
# Add your dotfiles here, then run `dotdeployer plan`:
#
#   [files]
#   "~/.zshrc"    = "shell/zshrc"                                  # symlink
#   "~/.gitconfig" = {{ source = "git/gitconfig", template = true }} # template
#   "~/.config/nvim" = "nvim"                                      # directory
#
#   [dirs]
#   "~/.local/bin" = {{}}
#
#   [profiles.work]
#   vars = {{ git_email = "you@company.com" }}

[files]
[dirs]

# Hooks run around every deployment, e.g. to reload a shell:
#   [hooks]
#   post_apply = ["./hooks/reload-shell.sh"]
'''

    return f'''# dotdeployer project: {name}
# Docs: https://github.com/hrfrutrd-dev/dotdeployer
version = 1

[settings]
source_root = "dotfiles"   # where the real dotfiles live
strategy    = "backup"     # backup | override | skip | ask | merge
prune       = true         # remove files that are no longer managed
hooks_enabled = true

[vars]
editor    = "nvim"
git_name  = ""             # override per machine: dotdeployer apply -V git_name="Ada Lovelace"
git_email = ""

[hooks]
# post_apply = ["./hooks/reload-shell.sh"]

[secrets]
order      = ["env", "file"]
env_prefix = "DOTDEPLOYER_SECRET_"
dir        = "secrets"

# --- entries ------------------------------------------------------------
# Short form: a table mapping target → source (optionally with settings).
[files]
"~/.zshenv"      = "shell/zshenv"
"~/.zshrc"       = {{ source = "shell/zshrc", template = true, tags = ["shell"] }}
"~/.gitconfig"   = {{ source = "git/gitconfig", template = true, tags = ["git"] }}
"~/.config/nvim" = {{ source = "nvim", mode = "symlink", tags = ["nvim"] }}

[dirs]
"~/.local/bin"   = {{}}

# Long form: full control per entry.
[[entry]]
name        = "ssh-config"
source      = "ssh_config"
target      = "~/.ssh/config"
mode        = "template"
permissions = "0600"
tags        = ["ssh"]
when        = "not is_container"
unless      = "exists('~/.ssh/config.keep')"
comment     = "Generated SSH client configuration"

# --- profiles -----------------------------------------------------------
[profiles.work]
description = "work laptop"
tags        = ["work", "git"]
vars        = {{ git_email = "you@company.com" }}

[profiles.personal]
description = "personal machines"
extends     = []
vars        = {{ git_email = "you@example.com" }}
'''


def cmd_plan(args: argparse.Namespace, console: Console) -> int:
    project = load_project(args, console, dry_run=True)
    plan = project.plan(
        **selection_kwargs(args),
        with_diffs=not getattr(args, "no_diff", False),
    )
    show_diff = bool(getattr(args, "show_diff", False))

    if args.json:
        emit_json(plan.to_dict(include_diff=True))
    else:
        console.heading(
            f"{project.config.facts.hostname}"
            + (f" · profiles: {', '.join(project.config.active_profiles())}"
               if project.config.active_profiles() else "")
        )
        console.blank()
        plan.render(
            console,
            show_diff=show_diff or bool(getattr(args, "show_all", False)),
            show_unchanged=bool(getattr(args, "show_all", False)),
            limit=getattr(args, "limit", None),
        )
        console.blank()
        plan.render_summary(console)
        for warning in plan.warnings:
            console.warn(warning)

    if getattr(args, "check", False) or getattr(args, "strict", False):
        strict = bool(getattr(args, "strict", False))
        if plan.errors:
            return EXIT_ERROR
        if plan.conflicts:
            return EXIT_CONFLICT
        if strict and plan.has_drift:
            return EXIT_DRIFT
        if plan.mutating:
            return EXIT_PENDING
    return EXIT_OK


def cmd_apply(args: argparse.Namespace, console: Console) -> int:
    project = load_project(args, console)
    plan = project.plan(**selection_kwargs(args))

    if plan.errors and not any(c.entry for c in plan.errors):
        for change in plan.errors:
            console.error(f"{display_path(change.target)}: {change.error}")
        return EXIT_ERROR

    if args.check:
        if args.json:
            emit_json(plan.to_dict())
        else:
            plan.render(console, show_diff=False)
            console.blank()
            plan.render_summary(console)
        if plan.errors:
            return EXIT_ERROR
        if plan.conflicts:
            return EXIT_CONFLICT
        if plan.has_drift:
            return EXIT_DRIFT
        return EXIT_PENDING if plan.mutating else EXIT_OK

    if not console.quiet:
        console.heading(f"applying to {project.config.facts.hostname}")
        console.blank()
        plan.render(console, show_diff=False, group=True, limit=200)
        console.blank()

    result = project.apply(
        plan,
        prune=not args.no_prune,
        backup=not args.no_backup,
        fail_fast=args.fail_fast,
        confirm=args.confirm,
        with_hooks=not args.no_hooks,
    )
    return _report_apply(result, console, json_output=args.json)


def _report_apply(result: ApplyResult, console: Console, *, json_output: bool) -> int:
    if json_output:
        emit_json(result.to_dict())
        return result.exit_code()

    if not console.quiet:
        console.blank()
    if not console.quiet and result.dry_run:
        console.write(console.paint("dry run - nothing was changed", Style.YELLOW), verbosity=0)
    counts = result.planned_counts or result.plan.counts()
    summary = " · ".join(f"{counts[status]} {status}" for status in
                         ("create", "update", "drift", "conflict", "remove", "skip", "unchanged", "error")
                         if counts.get(status))
    if not console.quiet:
        console.rule("summary")
        console.kv("result", result.summary_line())
        if summary:
            console.kv("plan", summary)
        if result.backup_id:
            console.kv("backups", str(result.backup_id))
        console.kv("duration", f"{result.duration_ms:.0f} ms")
        if result.hooks.get("total"):
            console.kv("hooks", f"{result.hooks['total']} run, {result.hooks.get('failed', 0)} failed")
    if result.errors:
        for change in result.errors:
            console.error(f"{display_path(change.target)}: {change.error}")
        console.warn(f"{len(result.errors)} entr(ies) failed - remaining entries were still processed")
        return EXIT_ERROR
    if not console.quiet:
        if result.applied or result.removals:
            console.ok("apply finished successfully")
        else:
            console.ok("already up to date")
    for warning in result.plan.warnings:
        console.warn(warning)
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace, console: Console) -> int:
    project = load_project(args, console, dry_run=True)
    checks = project.doctor()
    if args.no_drift:
        checks = [c for c in checks if c.category != "drift"]
    counts = summarise(checks)
    status = worst_status(checks)

    if args.json:
        emit_json({"status": status, "counts": counts, "checks": [c.to_dict() for c in checks]})
    else:
        render_checks(checks, console, show_info=args.show_info)
        console.blank()
        console.rule("diagnosis")
        console.kv("passed", counts.get("ok", 0))
        console.kv("warnings", counts.get("warn", 0))
        console.kv("failures", counts.get("fail", 0))
        console.kv("checks", len(checks))
        console.blank()
        if status == "fail":
            console.error("problems found - fix the failures above before applying")
        elif status == "warn":
            console.warn("warnings only - apply is possible but review them first")
        else:
            console.ok("project looks healthy")

    if status == "fail":
        return EXIT_ERROR
    if status == "warn" and args.strict:
        return EXIT_ERROR
    return EXIT_OK


def cmd_list(args: argparse.Namespace, console: Console) -> int:
    project = load_project(args, console, dry_run=True)
    entries = project.select(tags=args.tag) if args.tag else list(project.config.entries)
    statuses: Dict[str, str] = {}
    if args.status:
        for change in project.plan(with_diffs=False).all_changes():
            statuses[change.entry.key] = change.status

    rows = []
    for entry in entries:
        row = [
            entry.key,
            display_path(entry.target),
            display_path(entry.source_path) if entry.source_path else "-",
            entry.mode,
            ",".join(entry.tags) or "-",
            statuses.get(entry.key, ""),
        ]
        rows.append(row)

    if args.json:
        emit_json(
            {
                "count": len(entries),
                "entries": [
                    {**entry.to_dict(), "status": statuses.get(entry.key)} for entry in entries
                ],
            }
        )
        return EXIT_OK

    console.heading(f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'}")
    console.blank()
    console.table(
        ["name", "target", "source", "mode", "tags"] + (["status"] if args.status else []),
        rows,
    )
    return EXIT_OK


def cmd_info(args: argparse.Namespace, console: Console) -> int:
    project = load_project(args, console, dry_run=True)
    info = project.info()
    if args.json:
        emit_json(info)
        return EXIT_OK
    console.heading("project")
    for key, value in info["config"].items():
        if value in (None, "", []):
            continue
        console.kv(key, value)
    console.blank()
    console.heading("state")
    for key, value in info["state"].items():
        console.kv(key, value)
    console.blank()
    console.heading("backups")
    console.kv("sets", info["backups"]["count"])
    console.kv("size", human_size(info["backups"]["size"]).strip())
    console.kv("dir", display_path(info["backups"]["dir"]))
    console.blank()
    console.heading("secrets")
    console.kv("active", ", ".join(info["secrets"]["active"]) or "none")
    for warning in info["secrets"]["warnings"]:
        console.warn(warning)
    if info["hooks"]:
        console.blank()
        console.heading("hooks")
        for phase, count in sorted(info["hooks"].items()):
            console.kv(phase, count)
    return EXIT_OK


def cmd_tags(args: argparse.Namespace, console: Console) -> int:
    project = load_project(args, console, dry_run=True)
    mapping: Dict[str, List[str]] = {}
    for entry in project.config.entries:
        for tag in entry.tags or ["(untagged)"]:
            mapping.setdefault(tag, []).append(entry.key)
    if args.json:
        emit_json(mapping)
        return EXIT_OK
    console.heading(f"{len(mapping)} tag(s)")
    console.blank()
    console.table(["tag", "entries"], [[tag, ", ".join(keys)] for tag, keys in sorted(mapping.items())])
    return EXIT_OK


def cmd_profiles(args: argparse.Namespace, console: Console) -> int:
    project = load_project(args, console, dry_run=True, )
    profiles = project.config.profiles
    if args.activate:
        profiles = {
            name: profile for name, profile in profiles.items() if name == args.activate
        }
        if not profiles:
            raise SelectionError(f"unknown profile {args.activate!r}")
    if args.json:
        emit_json(
            {
                "active": project.config.active_profiles(),
                "profiles": {
                    name: {
                        "description": profile.description,
                        "tags": profile.tags,
                        "entries": profile.entries,
                        "extends": profile.extends,
                        "vars": profile.vars,
                    }
                    for name, profile in profiles.items()
                },
            }
        )
        return EXIT_OK
    if not profiles:
        console.info("no profiles defined")
        return EXIT_OK
    console.heading("profiles")
    console.blank()
    rows = []
    for name, profile in sorted(profiles.items()):
        marker = "*" if name in project.config.active_profiles() else " "
        rows.append([
            f"{marker} {name}",
            profile.description or "-",
            ",".join(profile.tags) or "-",
            ",".join(profile.entries) or "-",
            ",".join(profile.extends) or "-",
        ])
    console.table(["name", "description", "adds tags", "enables entries", "extends"], rows)
    if args.activate:
        console.blank()
        console.heading(f"entries with profile {args.activate}")
        for entry in project.select():
            console.write(f"  • {entry.key}  {display_path(entry.target)}", verbosity=0)
    return EXIT_OK


def cmd_rollback(args: argparse.Namespace, console: Console) -> int:
    project = load_project(args, console)
    if args.list_only:
        data = project.rollback(list_only=True)
        if args.json:
            emit_json(data)
            return EXIT_OK
        sets = data["sets"]
        if not sets:
            console.info("no backup sets yet")
            return EXIT_OK
        console.heading(f"{len(sets)} backup set(s) · {human_size(data['total_size']).strip()}")
        console.blank()
        console.table(
            ["id", "items", "created", "reason"],
            [[item["id"], item.get("count", 0), item.get("created_at", ""), item.get("reason", "")]
             for item in sets],
        )
        return EXIT_OK

    if not project.dry_run and project.backups.sets():
        newest = project.backups.sets()[0]["id"]
        target = args.backup_id or newest
        if not confirm_destructive(args, console, f"Restore backup {target} (current files are replaced)?"):
            console.warn("aborted")
            return EXIT_OK

    data = project.rollback(args.backup_id, keep_state=args.keep_state)
    if args.json:
        emit_json(data)
        return EXIT_OK
    console.blank()
    if not data["restored"]:
        console.warn(f"backup {data['backup']} contained nothing restorable")
        return EXIT_OK
    console.ok(f"restored {len(data['restored'])} item(s) from {data['backup']}")
    for path in data["restored"][:20]:
        console.write(f"  • {display_path(path)}", verbosity=0)
    if data.get("forgotten"):
        console.note(f"forgot {len(data['forgotten'])} state entr(ies); run apply to redeploy them")
    return EXIT_OK


def cmd_backup_ls(args: argparse.Namespace, console: Console) -> int:
    args.list_only = True
    return cmd_rollback(args, console)


def cmd_backup_purge(args: argparse.Namespace, console: Console) -> int:
    project = load_project(args, console)
    keep = args.keep if args.keep is not None else project.config.settings["backup_keep"]
    removals = project.backups.purge(keep=keep, dry_run=project.dry_run)
    if args.json:
        emit_json({"kept": keep, "removed": removals, "dry_run": project.dry_run})
        return EXIT_OK
    if not removals:
        console.info(f"nothing to purge (keeping {keep} set(s))")
        return EXIT_OK
    verb = "would remove" if project.dry_run else "removed"
    console.ok(f"{verb} {len(removals)} backup set(s): {', '.join(removals)}")
    return EXIT_OK


def cmd_uninstall(args: argparse.Namespace, console: Console) -> int:
    project = load_project(args, console)
    tracked = list(project.state.entries.values())
    if not tracked:
        console.info("nothing is deployed")
        return EXIT_OK
    if not project.dry_run and not confirm_destructive(
        args, console, f"Remove {len(tracked)} deployed target(s)?"
    ):
        console.warn("aborted")
        return EXIT_OK
    changes = project.uninstall(
        backup=not args.keep_backups, keep_state=args.keep_state, only=args.only
    )
    if args.json:
        emit_json({"removed": [c.to_dict() for c in changes]})
        return EXIT_OK
    removed = [c for c in changes if c.status == "remove"]
    drifted = [c for c in changes if c.status == "drift"]
    console.blank()
    if project.dry_run:
        console.info(f"would remove {len(removed)} target(s) (dry-run, nothing changed)")
    else:
        console.ok(f"removed {len(removed)} target(s)")
    if drifted:
        console.warn(f"{len(drifted)} target(s) kept because they were modified locally")
        for change in drifted[:10]:
            console.write(f"  • {display_path(change.target)}", verbosity=0)
    return EXIT_OK


def cmd_adopt(args: argparse.Namespace, console: Console) -> int:
    project = load_project(args, console)
    adopted = project.adopt(args.targets, move=not args.copy, mode=args.mode, tags=args.tag)
    if args.json:
        emit_json({"adopted": [entry.to_dict() for entry in adopted]})
        return EXIT_OK
    if not adopted:
        console.warn("nothing was adopted")
        return EXIT_OK
    console.blank()
    console.ok(f"adopted {len(adopted)} path(s) into {display_path(project.config.source_root)}")
    console.note("next: dotdeployer plan --diff")
    return EXIT_OK


def _referenced_secrets(project: Project) -> Dict[str, List[str]]:
    refs: Dict[str, List[str]] = {}
    for entry in project.config.entries:
        for name in entry.secrets:
            refs.setdefault(name, []).append(entry.key)
    return refs


def cmd_secrets_list(args: argparse.Namespace, console: Console) -> int:
    project = load_project(args, console, dry_run=True)
    refs = _referenced_secrets(project)
    resolved = {}
    for name in refs:
        value = project.secrets.resolve_optional(name)
        resolved[name] = value
    data = {
        "backends": project.secrets.describe(),
        "referenced": {
            name: {
                "entries": entries,
                "resolved": resolved[name] is not None,
                "length": len(resolved[name]) if resolved[name] else 0,
            }
            for name, entries in refs.items()
        },
        "warnings": project.secrets.warnings,
    }
    if args.json:
        emit_json(data)
        return EXIT_OK
    console.heading("secret backends")
    console.kv("order", ", ".join(data["backends"]["order"]) or "-")
    console.kv("active", ", ".join(data["backends"]["active"]) or "none")
    for warning in data["backends"]["warnings"]:
        console.warn(warning)
    console.blank()
    if not refs:
        console.info("no entry references secrets (add secrets = [\"name\"] to an entry)")
        return EXIT_OK
    console.heading(f"{len(refs)} referenced secret(s)")
    console.blank()
    rows = []
    for name, entries in sorted(refs.items()):
        status = "resolved" if resolved[name] is not None else "missing"
        preview = (("*" * min(8, len(resolved[name]))) if resolved[name] and args.values else "")
        rows.append([name, status, str(len(resolved[name]) if resolved[name] else 0),
                     preview, ", ".join(entries)])
    console.table(["secret", "state", "chars", "preview", "used by"], rows)
    missing = [name for name in refs if resolved[name] is None]
    if missing:
        console.blank()
        console.warn(f"{len(missing)} secret(s) cannot be resolved: {', '.join(missing)}")
        console.info("run 'dotdeployer secrets check' to fail hard, or add template fallbacks")
    return EXIT_OK


def cmd_secrets_check(args: argparse.Namespace, console: Console) -> int:
    args.values = False
    project = load_project(args, console, dry_run=True)
    refs = _referenced_secrets(project)
    missing = [name for name in refs if project.secrets.resolve_optional(name) is None]
    if args.json:
        emit_json({"referenced": sorted(refs), "missing": missing})
        return EXIT_ERROR if missing else EXIT_OK
    if missing:
        console.error(f"unresolved secret(s): {', '.join(missing)}")
        console.info("configure a backend in [secrets] or export DOTDEPLOYER_SECRET_<NAME>")
        return EXIT_ERROR
    console.ok(f"all {len(refs)} referenced secret(s) resolved")
    return EXIT_OK


def cmd_encrypt(args: argparse.Namespace, console: Console) -> int:
    path = Path(args.file).expanduser()
    if not path.is_file():
        raise ActionError(f"file not found: {path}")
    data = path.read_bytes()
    project = None
    try:
        project = load_project(args, console, dry_run=True)
    except ConfigError:
        project = None
    secrets_config = project.config.secrets_config if project else {}
    root = project.config.root if project else path.parent
    method = str(secrets_config.get("encryption") or "gpg")
    encrypted = encrypt_bytes(data, options=secrets_config, root=root)
    suffix = ".age" if method == "age" or bytes(encrypted[:4]) == b"age" else ".gpg"
    output = Path(args.output).expanduser() if args.output else path.with_name(path.name + suffix)
    output.write_bytes(encrypted)
    if args.in_place:
        path.unlink()
    console.ok(f"encrypted {display_path(path)} → {display_path(output)}")
    console.note("reference it with encrypt = true on the entry")
    return EXIT_OK


def cmd_hook(args: argparse.Namespace, console: Console) -> int:
    project = load_project(args, console)
    context: Dict[str, Any] = {"phase": args.phase}
    if args.entry:
        entry = project.config.entry_by_name(args.entry)
        if entry is None:
            raise SelectionError(f"unknown entry {args.entry!r}")
        context.update({"entry": entry.key, "target": str(entry.target), "mode": entry.mode})
    runner = HookRunner(project.config, console=console, dry_run=project.dry_run)
    runs = runner.run(args.phase, context)
    if not runs:
        console.info(f"no hooks registered for phase {args.phase!r}")
        return EXIT_OK
    if args.json:
        emit_json({"runs": [{"phase": r.phase, "command": r.command,
                             "returncode": r.result.returncode if r.result else None,
                             "skipped": r.skipped, "error": r.error} for r in runs]})
    else:
        console.blank()
        console.ok(f"ran {len(runs)} hook(s) for {args.phase}")
    return EXIT_ERROR if any(r.result is not None and not r.result.ok for r in runs) else EXIT_OK


def cmd_report(args: argparse.Namespace, console: Console) -> int:
    project = load_project(args, console, dry_run=True)
    fmt = args.format or "html"
    output = Path(args.output) if args.output else Path.cwd() / f"dotdeployer-report.{fmt}"
    plan = project.plan(with_diffs=not args.no_diff)
    destination = write_report(project, output, fmt=fmt, include_diff=not args.no_diff, plan=plan)
    if str(destination) == "-":  # the document itself is the only thing on stdout
        return EXIT_OK
    if args.json:
        emit_json({"report": str(destination), "format": fmt, "counts": plan.counts()})
        return EXIT_OK
    console.ok(f"report written to {display_path(destination)}")
    console.note(plan.summary_line())
    return EXIT_OK


def cmd_config_path(args: argparse.Namespace, console: Console) -> int:
    project = load_project(args, console, dry_run=True)
    if args.json:
        emit_json({"config": str(project.config.path), "root": str(project.config.root)})
    else:
        console.write(str(project.config.path), verbosity=0)
    return EXIT_OK


def cmd_config_show(args: argparse.Namespace, console: Console) -> int:
    project = load_project(args, console, dry_run=True)
    data = project.config.to_dict()
    if args.json:
        emit_json(data)
        return EXIT_OK
    console.heading(f"config: {display_path(project.config.path)}")
    console.blank()
    console.heading("settings")
    for key, value in sorted(data["settings"].items()):
        console.kv(key, value)
    console.blank()
    console.heading(f"variables ({len(data['variables'])})")
    for key, value in sorted(data["variables"].items()):
        if key in {"has", "has_all", "has_any", "command_ok", "command_output", "package_installed",
                   "path", "exists", "env", "facts", "which"}:
            continue
        console.kv(key, truncate(str(value), 60))
    console.blank()
    console.heading(f"entries ({len(data['entries'])})")
    console.blank()
    rows = []
    for entry in data["entries"]:
        rows.append([
            entry["name"],
            display_path(Path(entry["resolved_target"])) if args.resolved else entry["target"],
            display_path(Path(entry["resolved_source"])) if args.resolved and entry["resolved_source"]
            else (entry["source"] or "-"),
            entry["mode"],
            ",".join(entry["tags"]) or "-",
        ])
    console.table(["name", "target", "source", "mode", "tags"], rows)
    return EXIT_OK


def cmd_config_validate(args: argparse.Namespace, console: Console) -> int:
    project = load_project(args, console, dry_run=True)
    problems = [c for c in project.doctor() if c.status == "fail"]
    if args.json:
        emit_json({"valid": not problems, "problems": [c.to_dict() for c in problems]})
    if problems:
        console.error("configuration is invalid")
        for check in problems:
            console.write(f"  • {check.name}: {check.message}", verbosity=0)
            if check.hint:
                console.write(f"    → {check.hint}", verbosity=0)
        return EXIT_ERROR
    console.ok("configuration is valid")
    return EXIT_OK


def cmd_completion(args: argparse.Namespace, console: Console) -> int:
    commands = " ".join(COMMANDS)
    if args.shell == "bash":
        script = f"""# bash completion for {PROG}
_{PROG}_complete() {{
  local cur prev
  cur="${{COMP_WORDS[COMP_CWORD]}}"
  prev="${{COMP_WORDS[COMP_CWORD-1]}}"
  case "$prev" in
    --profile|-p) COMPREPLY=( $(compgen -W "$({PROG} profiles 2>/dev/null | awk 'NR>2 {{print $1}}')" -- "$cur") ); return ;;
    --tag) COMPREPLY=( $(compgen -W "$({PROG} tags 2>/dev/null | awk 'NR>2 {{print $1}}')" -- "$cur") ); return ;;
    --config|-c|-o|--output|--var|-V) COMPREPLY=( $(compgen -f -- "$cur") ); return ;;
  esac
  COMPREPLY=( $(compgen -W "{commands} -c --config -p --profile -V --var -n --dry-run -y --yes --json -q --quiet -v --verbose --no-color --force --no-hooks" -- "$cur") )
}}
complete -F _{PROG}_complete {PROG}
complete -F _{PROG}_complete dd
"""
    elif args.shell == "zsh":
        script = f"""#compdef {PROG}
_{PROG}() {{
  local -a commands
  commands=({' '.join(COMMANDS)})
  _arguments -C \\
    '-c[config path]:path:_files' \\
    '-p[profile]:profile:($({PROG} profiles 2>/dev/null | awk 'NR>2 {{print $1}}'))' \\
    '--json[machine readable output]' \\
    '(-n --dry-run)'{{-n,--dry-run}}'[preview changes]' \\
    '(-y --yes)'{{-y,--yes}}'[assume yes]' \\
    '1:command:($commands)' \\
    '*::arg:->args'
}}
compdef _{PROG} {PROG}
"""
    else:
        script = f"""# fish completion for {PROG}
complete -c {PROG} -f -n '__fish_use_subcommand' -a '{commands}'
complete -c {PROG} -l json -d 'machine readable output'
complete -c {PROG} -s n -l dry-run -d 'preview changes'
complete -c {PROG} -s y -l yes -d 'assume yes'
complete -c {PROG} -s p -l profile -d 'activate a profile' -x
complete -c {PROG} -s V -l var -d 'override a variable' -x
complete -c {PROG} -s c -l config -d 'config path' -r
"""
    console.write(script, end="", verbosity=0)
    return EXIT_OK


def cmd_version(args: argparse.Namespace, console: Console) -> int:
    import platform

    data = {
        "version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
    }
    if args.json:
        emit_json(data)
        return EXIT_OK
    console.write(f"{PROG} {__version__}", verbosity=0)
    console.write(f"python {data['python']} on {data['platform']}", verbosity=0)
    return EXIT_OK


def cmd_help(args: argparse.Namespace, console: Console) -> int:
    parser: argparse.ArgumentParser = args._parser
    if not args.topic:
        parser.print_help()
        return EXIT_OK
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction) and args.topic in action.choices:
            action.choices[args.topic].print_help()
            return EXIT_OK
    console.error(f"unknown command: {args.topic}")
    return EXIT_ERROR


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_OK

    console = build_console(args)
    try:
        return int(args.func(args, console) or 0)
    except DotDeployerError as exc:
        if getattr(args, "json", False):
            emit_json({"error": str(exc), "kind": type(exc).__name__, "hint": getattr(exc, "hint", None)})
        else:
            console.error(str(exc))
            if getattr(exc, "hint", None):
                console.write(f"  {console.paint('hint: ' + str(exc.hint), Style.GREY)}", verbosity=0)
        return getattr(exc, "exit_code", EXIT_ERROR)
    except BrokenPipeError:  # pragma: no cover - piping into head
        try:
            sys.stdout.close()
        except Exception:
            pass
        return EXIT_OK
    except KeyboardInterrupt:  # pragma: no cover
        console.write("")
        console.warn("interrupted")
        return 130


def entrypoint() -> None:  # pragma: no cover - console_script shim
    sys.exit(main())


if __name__ == "__main__":  # pragma: no cover
    entrypoint()

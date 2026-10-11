# Examples

Two projects you can poke at without touching your real `$HOME`. Both commands below are
read-only (`plan` never writes anything), so they are safe to run from a clone of this
repository.

```console
# tiny: one manifest, three entries
cd examples/minimal
dotdeployer plan --diff

# full: profiles, templates, secrets, hooks, host overrides, permissions
cd examples/full
dotdeployer plan --diff
dotdeployer apply -p work -n            # dry run of the 'work' profile
dotdeployer doctor
```

## minimal

The smallest useful project: one symlinked config, one templated one and one directory.

| File | What it shows |
| --- | --- |
| `dotdeployer.toml` | `[settings]`, `[vars]`, shorthand (`[files]`, `[dirs]`) tables |
| `dotfiles/shell/zshrc` | A plain source deployed as a symlink |
| `dotfiles/shell/zshenv` | A source deployed as a template |

## full

A realistic personal setup with every feature switched on at least once.

| File | What it shows |
| --- | --- |
| `dotdeployer.toml` | Profiles, tags, per-entry strategies, permissions, hooks, secrets |
| `dotfiles/git/gitconfig.tmpl` | Template with `{{ }}`, `{% if %}` and profile variables |
| `dotfiles/shell/zshrc.tmpl` | OS/host conditionals and a `secret()` lookup |
| `dotfiles/shell/aliases` | Plain symlink inside the same directory as the template |
| `dotfiles/nvim/init.lua` | Directory deployed as a symlink |
| `dotfiles/ssh/config.tmpl` | `permissions = "600"` and `strategy = "override"` |
| `hosts/laptop.toml` | Per-machine overrides picked up automatically by hostname |
| `hooks/reload-shell.sh` | `post_entry` hook that reacts to what was deployed |

To try the full example against a scratch home directory:

```console
cd examples/full
export HOME=/tmp/dd-example-home        # keep your real dotfiles safe
mkdir -p "$HOME"
PYTHONPATH=../.. python3 -m dotdeployer plan --diff
```

`examples/full/dotdeployer.toml` expects the two secrets below; without a backend they fall back
to a placeholder (and `doctor` reports a warning):

```console
export DOTDEPLOYER_github_token=ghp_example
export DOTDEPLOYER_shell_theme=dracula
```

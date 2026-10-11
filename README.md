# dotdeployer

**English** | [日本語](docs/README.ja.md)

Inspect the project you are about to deploy **before** it leaves your machine: run strict local preflight checks on the files that would enter the Git deploy context, audit committed credentials and environment-variable exposure, and optionally execute the real build command in a temporary copy.

Adapters: Render, Railway, Vercel, Cloudflare Workers / Pages (Wrangler), Google Apps Script (GAS).

```sh
# Install the CLI globally from this repository
npm install --global git+https://github.com/hrfrutrd-dev/dotdeployer.git

dotdeployer --help
dotdeployer check  . --provider vercel          # static preflight of config and files
dotdeployer audit  . --provider vercel --strict # credential / env-var audit (secret values never printed)
dotdeployer test   . --provider vercel --build  # same checks plus a real build in a temporary copy
dotdeployer emulate . --provider cloudflare     # Wrangler official dry-run (no deploy)
```

For local development, clone the repository, run `npm ci`, then `npm link` to expose the checked-out CLI on your `PATH`.

A `PASS` means "the local checks this tool performs have passed" — not "the production deploy will certainly succeed". dotdeployer is not a cloud runtime emulator.

## Documentation

| Document | Contents |
|---|---|
| [Documentation (English)](docs/README.md) / [日本語ドキュメント](docs/README.ja.md) | Full guide: installation, usage, security model, scope and limitations |
| [CLI reference](docs/cli.md) / [日本語](docs/cli.ja.md) | Every command, option, exit code, and output format |
| [Provider reference](docs/providers.md) / [日本語](docs/providers.ja.md) | What is checked per provider, `emulate` runners, finding codes |
| [Repository layout](docs/architecture.md) / [日本語](docs/architecture.ja.md) | Directory structure, module roles, extending the tool |

## Safety model in one paragraph

`check` never contacts a hosting API. `audit` never executes project code and never prints a matched secret value. `emulate` and `verify` run official provider CLIs without ever invoking `deploy` / `push`. A real remote operation happens only for an explicitly named non-production Cloudflare Workers environment, and only with both `--execute` and `--yes`. Builds (`--build`, `--install`, MCP `run_local_build`) execute repository code and dependency install scripts with your host permissions — use them only on code you trust.

## Requirements

Node.js 20 or newer.

## License

MIT — see [LICENSE](LICENSE).

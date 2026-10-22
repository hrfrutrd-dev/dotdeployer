# CLI リファレンス

[English](cli.md) | **日本語**

← [ドキュメント](README.ja.md)

`dotdeployer` CLI のコマンド・オプション・終了コード・出力形式の一覧です。

## 起動方法

```sh
# グローバルインストール（または npm link）後
dotdeployer <command> [path] [options]

# リポジトリから直接実行
node ./bin/dotdeployer.js <command> [path] [options]

# 全体ヘルプとバージョン
dotdeployer
dotdeployer --help
dotdeployer --version

# コマンド別ヘルプ
dotdeployer check --help
dotdeployer help deploy
```

不明なコマンド・未対応オプション・不正なオプションの組み合わせは、使い方を stderr に出力して終了コード `2` で終了します。レポートを出力するコマンドで `--json` を指定した場合、使用法エラーも stdout に JSON として出力します。

## コマンド

| コマンド | 目的 | プロバイダー / ネットワーク側のコード実行 |
|---|---|---|
| `check [path]` | 設定ファイルとデプロイ対象ファイルの静的検査。 | なし |
| `audit [path]` | credential と環境変数契約の監査。読み取り専用で、プロジェクトのコードを実行しません。 | なし |
| `test [path]` | `check` と同じ検査。`--build` を付けると一時コピー上で実際のビルドコマンドを実行します。 | `--build`（および `--install`）指定時のみ |
| `emulate [path] --provider <name>` | 公式 CLI がある場合はそれを使ったローカルビルド / 検証。デプロイはしません。 | あり（公式 CLI が必要） |
| `verify [path] --provider <name>` | ローカル検査に加えて公式 CLI の認証確認。アカウント identity の出力は抑制します。 | あり（公式 CLI が必要） |
| `deploy [path] --provider cloudflare --environment <name>` | 既定はシミュレーション。実際の非 production Workers デプロイは `--execute --yes` を指定した場合のみ。 | `--execute --yes` 指定時のみ |
| `providers [--json]` | 対応アダプターと設定ファイル形式の一覧。 | なし |
| `mcp` | Claude Code などから利用する MCP stdio サーバーを起動。 | なし |
| `help [command]` | 全体または指定したコマンドのヘルプを表示。`<command> --help` でも利用可能。 | なし |

## オプション

| オプション | 値 | 既定値 | 対象コマンド | 備考 |
|---|---|---|---|---|
| `[path]` | ディレクトリ | `.` | check, audit, test, emulate, verify, deploy | 位置引数のパス。`--path` および `--repo` とは併用できません。 |
| `--path <dir>` | ディレクトリ | `.` | check, audit, test, emulate, verify, deploy | 位置引数のパスおよび `--repo` とは併用できません。 |
| `--repo <owner/repo\|URL>` | GitHub リポジトリ | – | check, audit, test, emulate, verify | 一時ディレクトリへ clone します。`deploy` では使用できません。 |
| `--ref <branch-or-tag>` | Git ref | 既定ブランチ | `--repo` と併用 | `--ref` には `--repo` が必要です。 |
| `--provider <name>` | 下記参照 | `auto` | check, audit, test, emulate, verify, deploy | `verify` はプロバイダーを 1 つだけ指定する必要があります。`deploy` は `cloudflare` のみ対応します。 |
| `--plan <name>` | 任意のラベル | `free` | check, audit, test, emulate, verify, deploy | ローカルのポリシープロファイルであり、プロバイダー API への問い合わせではありません。 |
| `--strict` | フラグ | 無効 | check, audit, test, emulate, verify, deploy | すべての warning を error に格上げします。 |
| `--build` | フラグ | 無効 | check, test のみ | 一時コピー上でビルドコマンドを実行します。 |
| `--install` | フラグ | 無効 | `--build` と併用、または emulate / deploy | 一時コピー内で lockfile に従って依存を取得し、依存パッケージの install script を実行します。 |
| `--timeout <seconds>` | 1–1800 | 600（`verify` は 60） | check, test, emulate, verify, deploy | ビルド / runner の各処理に適用するタイムアウト。範囲外の値は使用法エラーになります。 |
| `--json` | フラグ | 無効 | レポートを出力する各コマンドと `providers`（`mcp` を除く） | 機械可読な JSON を 1 ドキュメントで出力します。使用法エラーも stdout に JSON で出力します。 |
| `--sarif` | フラグ | 無効 | check, audit のみ | SARIF 2.1.0。`--json` および `--build` とは併用できません。 |
| `--environment <name>`, `--env` | Wrangler 環境名 | – | deploy のみ | Wrangler 設定に存在する名前を指定します。production は指定できません。 |
| `--execute` | フラグ | 無効 | deploy のみ | 実際のリモートデプロイを実行します。`--yes` が必要です。 |
| `--yes`, `-y` | フラグ | 無効 | deploy のみ | 実デプロイを確認します。`--execute` が必要です。 |
| `-h`, `--help` | フラグ | – | 全コマンド | 使い方を表示して終了コード 0。 |
| `-V`, `--version` | フラグ | – | 全コマンド | バージョンを表示して終了コード 0。 |

`--provider` には `auto`、`all`、`render`、`railway`、`vercel`、`cloudflare`、`gas`、`generic` を指定できます。エイリアスは正規名に解決されます（`cloudflare-workers`、`cloudflare-pages`、`workers`、`pages` → `cloudflare` / `apps-script`、`google-apps-script` → `gas`）。

## 終了コード

| コード | 意味 |
|---|---|
| `0` | 検査を通過しました（`test` の場合は preflight が通過し、指定時はビルドも成功）。 |
| `1` | レポートが `ok` ではありません（error、または `--strict` により格上げされた warning。`test` ではビルドの失敗・未実行も含みます）。 |
| `2` | 使用法エラー（不明なオプション、不正な組み合わせ、範囲外の `--timeout`）または想定外の内部エラー。`--json` 指定時は stderr のメッセージではなく JSON オブジェクトとして stdout に出力します。 |

## 出力形式

**人間が読む形式（既定）** では、概要に続けて `✖|⚠|ℹ [CODE] <ファイル>: message` 形式で検出項目を 1 行ずつ出力し（audit の検出は `<ファイル>:<行>` も含みます）、修正案がある場合は `Fix:` を添えます。検査系のレポートは、PASS がローカルの preflight 結果でありクラウド上の挙動を保証しない旨の注意で終わります。`audit` は加えて、secret の値が出力されない旨を表示します。

**`--json`** はコマンドごとに 1 つの JSON ドキュメントを出力します。主なトップレベルキーは次のとおりです。

| コマンド | トップレベルキー |
|---|---|
| `check` | `version`, `projectRoot`, `provider`, `detectedProviders`, `plan`, `strict`, `ok`, `filesScanned`, `maxFileSizeBytes`, `summary`, `issues` |
| `audit` | `preflight`, `summary`, `audit`, `environment`, `auditIssues` と上記 `check` 相当のキー |
| `test` | `preflight`, `attempted`, `success`, `commands`, `issues` |
| `emulate` | `version`, `projectRoot`, `provider`, `plan`, `preflight`, `emulationLevel`, `limitations`, `runner`, `commands`, `localBuild`, `issues`, `success`, `ok` |
| `verify` | `version`, `projectRoot`, `provider`, `mode: "live-verification"`, `preflight`, `audit`, `authentication`, `commands`, `issues`, `ok` |
| `deploy` | `version`, `projectRoot`, `provider`, `environment`, `mode`（`simulation` / `execute`）, `deployed`, `ok`, `verification`, `environmentDryRunPassed`, `commands`, `issues` |
| `providers` | `providers`, `fileSizeLimitBytes` |

`summary` は常に `{ "errors": n, "warnings": n, "infos": n }` です。`audit.audit` は `filesInContext`、`scannedTextFiles`、`skippedBinaryFiles`、`scannedBytes`、`secretFindings`、`findingsTruncated` を返します。`environment` は `referenced`、`documented`、`configured` と、変数ごとの `variables`（name、public、sensitivity、documented、configured、sources）を返します。`authentication` は `{ checked, authenticated, identityOutputSuppressed }` です。

**`--sarif`** は `check` または `audit` 用の SARIF 2.1.0 JSON を出力し、GitHub Code Scanning などに渡せます。位置情報がある指摘は `file` / `line` として出力され、一致した secret の値は決して含まれません。

```sh
dotdeployer audit . --provider vercel --sarif > dotdeployer.sarif
```

## コマンドの詳細

### `check`

自動検出または明示指定されたプロバイダー設定を解析し、設定が参照するパスを検証し、解析より前にファイル単位 20 MB の上限を適用し、共通のプロジェクト検査（`package.json`、lockfile、`Dockerfile`）を実行します。プロバイダーへの通信は行いません。サイズ上限で処理を中止した場合、レポートの `stoppedBeforeConfigValidation` が真になります。

### `audit`

`check` の preflight を実行したうえで、secret と環境変数の監査を追加します。プロジェクトのコードは実行しません。高信頼度の credential パターン、password を埋め込んだ URL、テンプレート以外の `.env` にある機密リテラル値は、値そのものを出さず、ファイル名・行番号・ルール名だけで報告します。未文書化の環境変数は warning で、`--strict` では error になります。public prefix（`VITE_`、`NEXT_PUBLIC_` など）が付いた機密名は最初から error です。

### `test`

`check` を実行し、`--build` がある場合は次の順でビルドコマンドを決定します: プロバイダー設定の build command → `package.json` の `build` script → Dockerfile build。コマンドはデプロイ対象の一時コピー内で実行され、失敗・タイムアウト・コマンド未解決のいずれもコマンド全体の失敗になります。設定から特定できる生成物ディレクトリは、実行後に同じサイズ上限で再スキャンします。`--install` を付けると先に lockfile から依存を取得します。

### `emulate`

プロバイダーごとに実行内容が決まっています（詳細は[プロバイダー対応表](providers.ja.md)）。

| プロバイダー | 実行するコマンド | `emulationLevel` |
|---|---|---|
| `render` | `render --version`、`render blueprints validate <config>`、続いて Blueprint の build command | `official-blueprint-validation-plus-local-build` |
| `railway` | `railpack --version`、`railpack build …`（Dockerfile builder では `docker build`） | `local-railpack-image-build` / `local-docker-build` |
| `vercel` | `vercel --version`、`vercel build`（事前の `vercel link` が必要） | `official-vercel-build-output-api` |
| `cloudflare`（Workers） | `wrangler --version`、`wrangler deploy --dry-run --outdir <tmp>` | `official-wrangler-dry-run` |
| `cloudflare`（Pages） | 設定済みのローカル build のみ | `local-pages-build-only` |
| `gas` | 利用できる公式ローカル runtime がないため emulator としては失敗 | – |

CLI が見つからない場合はスキップではなく error になります。JSON の `limitations` には再現できなかった範囲が並びます。

### `verify`

`audit` を実行したうえで、プロバイダー CLI の認証確認を行います。

| プロバイダー | 確認コマンド |
|---|---|
| `render` | `render whoami` |
| `railway` | `railway whoami` |
| `vercel` | `vercel whoami` |
| `cloudflare` | `wrangler whoami` |
| `gas` | `clasp login --status` |

いずれも事前に `<cli> --version` を実行します。identity の出力と生ログはレポートから意図的に除外し、`authentication.identityOutputSuppressed` は常に `true` です。対応プロバイダーを 1 つだけ指定しない場合は `VERIFY_PROVIDER_REQUIRED` で失敗します。

### `deploy`

まずシミュレーションとして、ローカル preflight → secret audit → 認証確認 → 指定環境の upload-free dry-run → artifact のサイズ検査、の順に実行します。実デプロイにはさらに `--execute --yes`、設定に宣言された Wrangler 環境、Workers（Pages ではない）であること、環境名・実効 Worker 名が非 production であること、および先行する全ステップの成功が必要です。ブロック時のコードには `DEPLOY_PROVIDER_UNSUPPORTED`、`DEPLOY_ENVIRONMENT_REQUIRED`、`DEPLOY_CONFIRMATION_REQUIRED`、`PAGES_DEPLOY_UNSUPPORTED`、`WRANGLER_ENVIRONMENT_NOT_CONFIGURED`、`PRODUCTION_DEPLOY_BLOCKED` があります。シミュレーションでは publish コマンドは実行されず、`"planned": true` として一覧に表示されます。

### `providers`

アダプター一覧を表示します。`--json` では同じ内容を `providers` と `fileSizeLimitBytes` として出力します。free / paid の区別はポリシー上のラベルであり、各社の現在のクォータを再現したものではありません。

### `mcp`

stdio の MCP サーバー（`preflight_check`、`audit_project`、`verify_provider_access`、`run_local_build`、`emulate_project`、`list_providers`）を起動します。デプロイ用ツールは公開していません。クライアント設定例は[ドキュメント](README.ja.md#mcp-サーバー)を参照してください。

## レシピ

```sh
# CI のゲート: warning も含めて失敗にする
dotdeployer check . --provider vercel --strict --json

# Code Scanning へのアップロード用
dotdeployer audit . --provider vercel --sarif > dotdeployer.sarif

# 手元に clone せずに他者のブランチを確認
dotdeployer audit --repo owner/repository --ref feature/deploy --provider railway

# ビルドまで含めた完全なローカル再現（タイムアウトを延ばす）
dotdeployer test . --provider render --build --install --timeout 1800

# 認証情報の有無だけを、値を表示せずに確認
dotdeployer verify . --provider cloudflare

# staging Worker を dry-run してから、デプロイするか判断
dotdeployer deploy . --provider cloudflare --environment staging
dotdeployer deploy . --provider cloudflare --environment staging --execute --yes
```

## 関連ドキュメント

- [ドキュメント](README.ja.md) — 考え方、セキュリティモデル、検査範囲と限界
- [プロバイダー対応表](providers.ja.md) — サービスごとの検査項目と検出コード
- [リポジトリ構成](architecture.ja.md) — 各コマンドの実装場所

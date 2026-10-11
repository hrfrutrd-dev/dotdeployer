# dotdeployer

複数のホスティングサービスへデプロイする前に、**Git に載るプロジェクトをローカルで厳しめに検査し、必要なら実際のビルドコマンドを一時コピー上で実行する** CLI / MCP サーバーです。

対応アダプター: Render、Railway、Vercel、Cloudflare Workers / Pages（Wrangler）、Google Apps Script（GAS）。

> **重要:** ホスティング各社の実行環境・全設定・全プラン・クォータを完全に再現するクラウドエミュレーターではありません。オンライン環境との差をゼロにすることや、本番デプロイの成功を保証することはできません。設定ファイルの静的検査とローカルビルドで、デプロイ前に見つけられる問題を早めに検出するツールです。対応範囲と限界は必ず「検査範囲と限界」を確認してください。

## インストール

Node.js 20 以上が必要です。

```sh
git clone https://github.com/hrfrutrd-dev/dotdeployer.git
cd dotdeployer
npm ci
npm link
```

`npm link` を使わず、リポジトリから直接実行する場合:

```sh
node ./bin/dotdeployer.js check /path/to/project --provider vercel
```

## 基本的な使い方

```sh
# 設定ファイルから対象サービスを自動検出
dotdeployer check .

# 対象と無料プランの想定を明示
dotdeployer check . --provider render --plan free

# GitHub のブランチを一時 clone して検査（公開・非公開。認証は既存の Git 設定を使用）
dotdeployer check --repo owner/repository --ref main --provider railway

# JSON を出力。CI や別ツールから利用可能
dotdeployer check . --provider cloudflare --json

# 警告も失敗扱いにする
dotdeployer check . --provider vercel --strict

# 検査に加えて、一時コピー上でビルドを実行
dotdeployer test . --provider vercel --build

# lockfile に従って依存をインストールしてからビルド
dotdeployer test . --provider vercel --build --install --timeout 900

# 各社公式 CLI によるローカルのビルド / dry-run 検査（公式 CLI の事前インストールが必要）
dotdeployer emulate . --provider cloudflare --timeout 900
```

コマンド名は `check` と `test` のどちらでも同じです。`--build` を付けない限り、プロジェクトのビルドやインストールは実行しません。プロバイダー指定のない場合は、対応設定ファイルを自動検出し、複数見つかればそれらをすべて静的検査します。ビルド時に複数プロバイダーが検出された場合は、対象を一つに絞るため明示指定が必要です。

### 20 MB のハード上限

デプロイ対象の **個別ファイルが 20,000,000 bytes 以上** の場合、Git 上のソースは設定解析・ビルドより前に検査を停止します。これは合計サイズではなくファイル単位の固定上限です。Git リポジトリでは追跡ファイルと未追跡ファイルを対象にし、`.gitignore` 等で除外されたファイルは Git デプロイ対象に含めません。Git LFS ポインターは実データのサイズを確認できないため、エラーとして停止します。設定から特定できる静的 publish directory / Vercel output / Wrangler dry-run output も、ビルド後に同じ上限で再スキャンします。Docker image の全レイヤーや各社の隠し build artifact までサイズ検査できるわけではありません。

## 対応範囲

| サービス | 主に読む設定 | 代表的なローカル検査 |
|---|---|---|
| Render | `render.yaml` / `render.yml` | Blueprint の YAML、services / service type / envVars / rootDir / static publish path。Free 想定では persistent disk をエラーにします。 |
| Railway | `railway.json` / `railway.toml` | JSON / TOML、build / deploy の代表フィールド、healthcheck、再起動ポリシー、Dockerfile 参照。 |
| Vercel | `vercel.json` | JSON、build/output directory、rewrites / redirects / headers、functions の代表的な型。Free 想定で cron を使う場合は現在のプラン制限を別途確認する警告を出します。 |
| Cloudflare Workers / Pages | `wrangler.toml` / `wrangler.json` / `wrangler.jsonc` | TOML / JSON / JSONC、main / assets のパス、vars、build、互換日付。互換日付が未指定の場合はローカルポリシーでエラーにします。 |
| Google Apps Script | `appsscript.json` / `.clasp.json` | マニフェスト、`rootDir`、runtime、OAuth scopes、Web App 設定の代表項目。ビルド時に `.gs` の JavaScript 構文を解析しますが、Apps Script API を実行するものではありません。 |
| 共通 | `package.json`、lockfile、`Dockerfile` | JSON / scripts / package manager と lockfile の整合性、Dockerfile の `FROM` の有無。 |

### 公式ローカル runner を使う `emulate`

静的ルールだけでは再現度に限界があるため、`dotdeployer emulate --provider ...` は利用可能な範囲で各社または各社が採用する公式 CLI を呼び出します。対応 CLI がない場合は、独自の近似結果で PASS にせずエラーにします。

| 対象 | 実行するローカル検査 |
|---|---|
| Render | `render blueprints validate` と Blueprint の build command。Render CLI が必要です。 |
| Railway | `railpack build`（Dockerfile builder なら Docker build）。Railpack / Docker が必要です。 |
| Vercel | `vercel build`。事前に `vercel link` を実行し `.vercel/project.json` を用意してください。環境変数を pull する `--yes` は自動実行しません。 |
| Cloudflare Workers | `wrangler deploy --dry-run --outdir ...`。Wrangler の公式 bundler による dry-run で、デプロイはしません。 |
| Cloudflare Pages | 設定済みのローカル build を実行します。Pages の upload-free な deploy dry-run ではないため、実行レベルをレポートに明示します。 |
| GAS | 公式ローカル runtime がないため emulator としては失敗扱いにします。manifest / 構文検査は `check` / `test` で利用できます。 |

外部 CLI の検証は、機能によってプロバイダー API への読み取りアクセスや認証を必要とする場合があります。ビルドは依存パッケージ / コンテナイメージを取得するためネットワークを使い、Railpack は Docker CLI の registry credentials を使う場合があります。ビルドコードも実行されますが、dotdeployer 自身は **deploy / push コマンドを呼び出しません**。Vercel / Railway の認証設定がある場合も、`vercel pull` や `railway up` は実行しません。

```sh
# 例: Wrangler の official dry-run
dotdeployer emulate ./worker --provider cloudflare
```

このモードも本番の managed runtime、バインディング、secret、クォータなどすべてを複製するわけではありません。実行レベルは JSON の `emulationLevel`、未再現の範囲は `limitations` として返します。

`--plan free` / `--plan paid` は検査ポリシーのプロファイル名です。`--plan hobby`、`--plan pro` など任意の名前も受け付けますが、**プロバイダー API に問い合わせたり、すべてのプランの現在の機能・クォータを再現したりはしません**。無料枠と有料枠の差が明示的に検査されない項目は警告または未検証として扱います。プラン仕様は変更されるため、本番前に各社の最新ドキュメントと実アカウントでも確認してください。

## MCP サーバー

`dotdeployer mcp` は MCP の stdio サーバーを起動します。Claude Code などの MCP クライアントから、読み取り専用の `preflight_check`、任意実行の `run_local_build`、公式 CLI を使う `emulate_project`、`list_providers` を利用できます。`preflight_check` / `run_local_build` / `emulate_project` は GitHub の `repository` と任意の `ref` を受け付けます。`emulate_project` は対応 CLI が必要で、対応外の完全な runtime 再現を装って成功扱いにはしません。

Claude Code の MCP 設定例（`/absolute/path/to/dotdeployer` は実際のパスに置換）:

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

`run_local_build` と CLI の `--build` は、対象リポジトリのコマンドを実行します。`--install` を指定するとネットワークから依存を取得し、依存パッケージの lifecycle script も実行します。**信頼できるコードにだけ使用してください。** ビルドは一時ディレクトリのコピー上で行いますが、OS レベルのセキュリティサンドボックスではなく、ホストの権限・環境変数・ネットワークを利用できます。

## GitHub と設定読み込み

`--repo` は GitHub の `owner/repository`、HTTPS URL、SSH URL を受け付けます。認証情報は保存・要求せず、既存の Git credential helper / SSH agent を使います。clone 後に Git のデプロイ対象をスキャンし、上限を確認してから設定を解析します。サブディレクトリのプロジェクトは `--path` で直接指定して検査できます。

設定ファイルの解析には YAML / TOML / JSON / JSONC のパーサーを使用します。これは各社の全バージョンの schema と完全一致することを意味しません。新規・実験的な設定項目の一部は未検証です。

## 検査範囲と限界

- `check` は設定とファイルのローカル検査のみを行い、デプロイ API 呼び出し、認証検証、ホスティング環境へのアップロードは行いません。
- `--build` は設定に書かれた build command / `package.json` の `build` script / Dockerfile build のいずれかを一時コピー上で実行します。全プロバイダーの install / build / runtime / healthcheck 手順を再現するわけではありません。
- 管理された Node / Python / Go / Java ランタイム、ネットワーク、DNS、リージョン、環境変数・secret、OAuth 同意、サービス間通信、データベース、コールドスタート、CPU / メモリ / 実行時間 / cron / API クォータなどは完全には再現できません。
- 無料プランで静的に判断できる既知の制約のみを強制し、変更頻度の高いクォータや契約条件は推測でエラーにせず、警告・未検証にします。
- `--strict` は既知の警告をエラーに格上げします。プロバイダーに問い合わせて新しい事実を追加するものではありません。
- `--build` は本物の build script を実行します。リポジトリ由来のコードは信頼できる場合にだけ実行してください。

したがって、`PASS` は「このツールが実施したローカル検査を通過した」という意味であり、「本番デプロイの成功が確実」という意味ではありません。

## 開発

```sh
npm test
node ./bin/dotdeployer.js --help
```

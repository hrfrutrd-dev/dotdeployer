# プロバイダー対応表

[English](providers.md) | **日本語**

← [ドキュメント](README.ja.md)

ホスティングサービスごとに何を検査するか、`emulate` が使う公式 CLI、レポートに現れる検出コードの一覧です。

## アダプターと設定ファイルの検出

| プロバイダー | 設定ファイル | 静的検査の内容 |
|---|---|---|
| `render` | `render.yaml`、`render.yml` | Blueprint の services と service type、envVars と env group、`rootDir`、Dockerfile 参照、static publish path、Free プランの disk |
| `railway` | `railway.json`、`railway.toml` | `build` / `deploy` の各フィールド、builder 名、Dockerfile 参照、healthcheck のパスとタイムアウト、再起動ポリシー、レプリカ数 |
| `vercel` | `vercel.json` | build / install / dev / output directory、`framework`、rewrites と redirects、headers、functions、crons、env キー |
| `cloudflare` | `wrangler.toml`、`wrangler.json`、`wrangler.jsonc` | `name`、`main`、`pages_build_output_dir`、`compatibility_date` と flags、`vars`、`build.command`、`assets.directory`、名前付き環境 |
| `gas` | `appsscript.json`、`.clasp.json` | clasp の `rootDir`、マニフェストの `timeZone`、`runtimeVersion`、`exceptionLogging`、`oauthScopes`、`webapp` の access / executeAs |

検出はリポジトリ直下だけでなく、**デプロイ対象に含まれる任意の場所**のファイル名で行います。

- `--provider auto`（既定）と `--provider all` は検出されたすべてのプロバイダーを検査します。1 つも検出されない場合は info の `NO_PROVIDER_CONFIG_DETECTED` を出しつつ、共通検査は実行します。
- プロバイダーを明示した場合は設定ファイルがなくてもそのアダプターを検査します。Render / Railway / Vercel / Cloudflare では warning の `PROVIDER_CONFIG_MISSING`、Apps Script では error の `GAS_MANIFEST_MISSING` になります。
- `--provider generic` はプロバイダー設定を一切読まず、共通のプロジェクト検査のみを実行します。
- 同じプロバイダーの設定ファイルが複数ある場合は `MULTIPLE_PROVIDER_CONFIGS` の warning を出し、それぞれを独立に検査します。
- ビルドを行うときにプロバイダー（またはその設定ファイル）が複数検出されると、対象を明示しない限り `BUILD_PROVIDER_AMBIGUOUS` / `BUILD_PROVIDER_CONFIG_AMBIGUOUS` で停止します。

## 全プロバイダー共通の検査

**デプロイ対象の列挙。** Git リポジトリでは `git ls-files`（未追跡ファイルを含む）、それ以外ではディレクトリ走査でファイルを集めます。走査では `.git`、`node_modules`、`.venv`、`__pycache__`、`.next`、`.nuxt`、`.output`、`.svelte-kit`、`.turbo`、`.cache`、`coverage`、`dist`、`build` を無視します。プロジェクト外を指すシンボリックリンク、dangling link、ディレクトリへのリンクは `EXTERNAL_SYMLINK` で失敗します。20,000,000 bytes 以上のファイルが 1 つでもあると、設定を解析する前に `FILE_TOO_LARGE` で停止します。Git LFS ポインターと Docker イメージのレイヤーはこの検査の対象外です。

**`package.json` と lockfile。** `PACKAGE_JSON_INVALID`、`PACKAGE_JSON_SHAPE_INVALID`、`PACKAGE_SCRIPTS_INVALID`、`PACKAGE_SCRIPT_INVALID`、`PACKAGE_TYPE_INVALID`、`PACKAGE_ENGINES_INVALID`、`PACKAGE_NODE_ENGINE_INVALID`、`PACKAGE_MANAGER_INVALID`、`PACKAGE_MANAGER_UNSUPPORTED`、`PACKAGE_MANAGER_LOCK_MISSING` は error です。認識する lockfile は `package-lock.json`、`npm-shrinkwrap.json`、`pnpm-lock.yaml`、`yarn.lock`、`bun.lock`、`bun.lockb` で、パッケージマネージャーが競合すると `MULTIPLE_LOCKFILES` の warning、lockfile がないと `LOCKFILE_MISSING` の warning（`--strict` で error）になります。

**環境変数ファイル。** デプロイ対象にある `.env*` のうち `.env.example`、`.env.sample`、`.env.template`、`.env.defaults` 以外は `ENV_FILE_IN_DEPLOY_CONTEXT` の warning になります。

**Dockerfile。** `FROM` 命令がない `Dockerfile` は error（`DOCKERFILE_FROM_MISSING`）、読み取れない場合は `DOCKERFILE_READ_ERROR` です。プロバイダー設定から参照される Dockerfile は、存在することとデプロイ対象に含まれることの両方が必要で、含まれない場合は `DOCKERFILE_NOT_IN_DEPLOY_CONTEXT` になります。

**設定の解析。** `CONFIG_PARSE_ERROR`、`CONFIG_SHAPE_INVALID`、`CONFIG_FIELD_REQUIRED`、`CONFIG_FIELD_TYPE`、`CONFIG_READ_ERROR`、およびパス関連の `CONFIG_PATH_ESCAPES_PROJECT`、`CONFIG_PATH_NOT_FILE`、`CONFIG_PATH_NOT_DIRECTORY`、`CONFIG_PATH_NOT_FOUND`、`CONFIG_PATH_CHECK_FAILED` があります。プロジェクト外へ出るパスは常に error です。

## プランプロファイル

`--plan` はローカルのポリシープロファイルを選びます（既定は `free`）。各プロバイダーで「Free」として扱う名前は次のとおりです。

| プロバイダー | Free として扱う名前 |
|---|---|
| `render` | `free` |
| `railway` | `free`、`trial` |
| `vercel` | `free`、`hobby` |
| `cloudflare` | `free`、`workers free` |
| `gas` | `free`、`consumer` |

それ以外の値（`paid`、`pro`、`standard` など）は有料プロファイルになります。プロファイルが変えるのは静的に判定できる検査だけです。

| 検出 | 重要度 | 意味 |
|---|---|---|
| `PLAN_LIMIT_DISK` | Free で error | Render のサービスが `disk` を宣言しています。ローカルの Free プロファイルは persistent disk を許可しません。 |
| `PLAN_MISMATCH` | Free で error | Free プロファイルの選択中に、`plan` が `free` 以外の Render サービスが宣言されています。 |
| `PLAN_LIMIT_UNVERIFIED` | warning | プラン依存の可能性がある機能（Railway のレプリカ 2 以上、Vercel の cron、Cloudflare の独自 limits）。最新の条件を各社で確認してください。 |
| `PLAN_QUOTAS_NOT_SIMULATED` | info | Apps Script のクォータはオフラインで再現できません。 |

dotdeployer はプラン情報をプロバイダー API に問い合わせません。クォータ・料金・機能ゲートは頻繁に変わるため、静的に判定できない項目は error にせず warning または未検証として扱います。

## Render

- **設定:** `render.yaml` / `render.yml`。Blueprint には空でない `services` 配列が必要です（`RENDER_SERVICES_REQUIRED`）。
- **サービス:** 各要素はオブジェクトで、空でない `name` が必要です。`type` は `static`、`web`、`worker`、`cron`、`pserv` のいずれかです（`RENDER_SERVICE_INVALID`、`RENDER_SERVICE_TYPE_INVALID`）。
- **パス:** `rootDir` は相対パスで存在する必要があります。`dockerfilePath` / `dockerContext` も存在が必要です。`staticPublishPath` はサービスルートからの相対パスで、まだ存在しない場合はビルドで生成され得るため info になります。
- **環境変数:** `envVars` はスカラー値を持つオブジェクトの配列です（`RENDER_ENV_VAR_INVALID`）。キーが secret / token / password / private key / API key に一致すると `POSSIBLE_SECRET_IN_CONFIG` の warning、`fromGroup` が Blueprint 内で宣言されていないと `ENV_GROUP_NOT_DECLARED` の warning になります。
- **実行:** `startCommand` も `dockerCommand` もないサービスは、Render がコマンドを推測し得るため `RENDER_START_COMMAND_UNVERIFIED` の warning になります。
- **`emulate`:** Render CLI が必要です。`render --version`、`render blueprints validate <config>` を実行し、続けて Blueprint の build command を実行します。`emulationLevel` は `official-blueprint-validation-plus-local-build` です。複数サービスの Blueprint は部分的なビルドをせず失敗させます。
- **限界:** Render 側のビルドイメージ、インスタンス種別、ディスク、リージョンの挙動は再現しません。

## Railway

- **設定:** `railway.json` / `railway.toml`。`build` と `deploy` は存在する場合オブジェクトである必要があります。
- **ビルド:** `builder` は文字列で、未知の名前は `RAILWAY_BUILDER_UNKNOWN` の warning になります。このチェッカーが知っている名前は `DOCKERFILE` と `RAILPACK` です。`buildCommand`、`dockerfilePath`、`dockerContext` は存在確認とデプロイ対象への含有まで検査します。
- **デプロイ:** `startCommand`、`preDeployCommand`、`healthcheckPath`、`cronSchedule` は文字列です。`healthcheckPath` は `/` で始まる必要があり（`RAILWAY_HEALTHCHECK_PATH_INVALID`）、`healthcheckTimeout`、`restartPolicyMaxRetries`、`numReplicas` は整数、`restartPolicyType` は `ON_FAILURE`、`ALWAYS`、`NEVER` のいずれかです（`RAILWAY_RESTART_POLICY_INVALID`）。
- **実行:** `deploy.startCommand` も Dockerfile もない設定は `RAILWAY_START_COMMAND_UNVERIFIED` の warning になります。
- **`emulate`:** Railpack または Docker が必要です。`railpack --version` と `railpack build …`、Dockerfile builder の場合は Docker build を実行し、`emulationLevel` は `local-railpack-image-build` または `local-docker-build` になります。一時イメージは実行後に削除し、削除に失敗した場合は `DOCKER_IMAGE_CLEANUP_FAILED` を報告します。
- **限界:** Railway 側のマネージドビルド環境、registry credentials、ボリューム、ヘルスモニタリングはローカル実行の対象外です。

## Vercel

- **設定:** `vercel.json`。文字列フィールドは `buildCommand`、`installCommand`、`devCommand`、`outputDirectory`、`framework`、`ignoreCommand` で、`cleanUrls` と `trailingSlash` は boolean です。
- **ルーティング:** `rewrites`、`redirects`、`headers`、`routes`、`builds`、`crons` は配列である必要があります。rewrites と redirects には文字列の `source` と `destination` が必要で（`VERCEL_ROUTE_INVALID`）、header ルールには文字列の `source` と headers 配列が必要です（`VERCEL_HEADER_RULE_INVALID`）。
- **Functions:** glob をキーとするオブジェクトで、各要素の `maxDuration` と `memory` は正の整数です（`VERCEL_FUNCTION_CONFIG_INVALID`）。
- **出力:** `outputDirectory` はビルドで生成され得るため、存在しない場合は info になります。
- **Cron:** Free プロファイルでは `crons` が空でない場合に `PLAN_LIMIT_UNVERIFIED` を出します。
- **`emulate`:** Vercel CLI と事前の `vercel link` が必要です（未リンクは `VERCEL_PROJECT_NOT_LINKED`）。`vercel --version` と `vercel build` を実行し、`.vercel/output` の存在を確認します（`VERCEL_OUTPUT_MISSING` / `VERCEL_OUTPUT_INVALID`）。`emulationLevel` は `official-vercel-build-output-api` です。環境変数を取得する `--yes` は自動では付けません。
- **限界:** Vercel の Build Output API はローカルに再現しますが、エッジネットワークの挙動、リージョン、画像最適化は再現しません。

## Cloudflare Workers / Pages

- **設定:** `wrangler.toml`、`wrangler.json`、`wrangler.jsonc` を TOML / JSON / JSONC として解析します（解析・構造の失敗は `WRANGLER_CONFIG_INVALID`）。
- **識別子とエントリポイント:** `name` は存在する場合に空でない文字列である必要があります。`main` は文字列で、ファイルが存在しなければなりません（`CONFIG_PATH_NOT_FOUND`）。
- **互換性:** `compatibility_date` は実在する `YYYY-MM-DD` の日付である必要があり（`CLOUDFLARE_COMPATIBILITY_DATE_INVALID`）、このチェッカーのポリシーでは**必須**です（`CLOUDFLARE_COMPATIBILITY_DATE_REQUIRED`）。`compatibility_flags` は文字列の配列です。
- **vars と assets:** `vars` は文字列値をとるオブジェクトである必要があり（`CLOUDFLARE_VAR_INVALID`）、機密らしいキーは `vars` が平文の設定であるため `POSSIBLE_SECRET_IN_CONFIG` の warning になります。`assets.directory` と `pages_build_output_dir` は存在を確認し、ビルドで生成され得るものは info です。
- **名前付き環境:** 各 `env.<name>` オブジェクトにも、型・パス・互換性・vars・build・assets・プランプロファイルの検査を適用します。検出メッセージのフィールドには `env.<name>.` を付け、不正な環境エントリーは `CLOUDFLARE_ENVIRONMENT_INVALID` とします。環境側で省略された互換性設定は、必須のトップレベル設定を継承します。
- **Pages:** `pages_build_output_dir` を使うプロジェクトは Pages として扱います。`emulate` は設定済みのローカル build のみを実行し（`emulationLevel: local-pages-build-only`）、upload-free な Pages の deploy dry-run ではないことを `PAGES_RUNTIME_NOT_EMULATED` で記録します。`deploy` は Pages を完全に拒否します（`PAGES_DEPLOY_UNSUPPORTED`）。
- **`emulate`（Workers）:** Wrangler が必要です。設定ファイルのあるディレクトリから `wrangler --version` と `wrangler deploy --dry-run --outdir <tmp>` を実行し、`emulationLevel` は `official-wrangler-dry-run` です。生成物は 20 MB の上限でサイズ検査します。`main` のない assets-only Worker もこの経路を使い、Pages の build 経路は明示的な Pages output 設定だけで選択します。
- **`deploy`:** 実行パスを持つ唯一のプロバイダーです。Wrangler 設定が 1 つであること、環境が宣言されていること（`WRANGLER_ENVIRONMENT_NOT_CONFIGURED`、`WRANGLER_ENVIRONMENT_INVALID`）、Pages でないこと、環境名・継承後の名前を含む実効 Worker 名が production でないこと（`PRODUCTION_DEPLOY_BLOCKED`）が必要です。`--execute --yes` がない場合、publish コマンドは planned として表示されるだけです。
- **限界:** バインディング（KV、D1、R2、Queues、Durable Objects）、service bindings、secret、Workers ランタイムの挙動は再現しません。

## Google Apps Script

- **設定:** `.clasp.json` と `appsscript.json`。`rootDir` は相対パスでプロジェクト内に存在する必要があります。マニフェストはそのルート配下から探し、見つからない場合は `GAS_MANIFEST_MISSING` です。
- **マニフェスト:** `timeZone` は必須です。`runtimeVersion` は `V8` または `DEPRECATED_ES5` で（`GAS_RUNTIME_INVALID`）、ES5 では `GAS_RUNTIME_DEPRECATED` の warning、未指定では `GAS_RUNTIME_UNPINNED` の warning になります。`exceptionLogging` は `STACKDRIVER`、`CLOUD_LOGGING`、`NONE` のいずれか、`oauthScopes` は HTTPS URL の配列です（`GAS_OAUTH_SCOPES_INVALID`）。
- **Web アプリ:** `webapp.access` は `MYSELF`、`DOMAIN`、`ANYONE`、`ANYONE_ANONYMOUS`（`GAS_WEBAPP_ACCESS_INVALID`）、`webapp.executeAs` は `USER_ACCESSING` または `USER_DEPLOYING` です（`GAS_WEBAPP_EXECUTION_INVALID`）。
- **ビルド:** 実際のビルドはありません。`--build` を付けると、マニフェストと同じ階層の `.gs` を JavaScript として解析し（`GAS_SYNTAX_OK`、`GAS_SYNTAX_ERROR`）、ソースがない場合は `GAS_SOURCE_NOT_FOUND` の warning になります。Apps Script API は呼び出しません。
- **`emulate`:** 公式のローカル runtime がないため失敗します。マニフェストと構文の検査には `check` / `test` を使ってください。
- **`verify`:** `clasp login --status`（および `clasp --version`）を実行し、identity の出力は表示しません。
- **限界:** OAuth 同意、クォータ、トリガー、コンテナバインドの挙動はオフラインで再現できません。

## secret と環境変数契約

`audit` は次の検査を追加します。セキュリティモデルは[ドキュメント](README.ja.md#デプロイセキュリティ監査と環境変数契約)を参照してください。

| 検出 | 重要度 | 契機 |
|---|---|---|
| `SECRET_PRIVATE_KEY` | error | デプロイ対象テキスト内の PEM 秘密鍵ブロック |
| `SECRET_AWS_ACCESS_KEY` | error | AWS access key ID に一致する値 |
| `SECRET_GITHUB_TOKEN` | error | GitHub のトークン形式（`ghp_`、`gho_`、`ghu_`、`ghs_`、`ghr_`、`github_pat_`） |
| `SECRET_GITLAB_TOKEN` | error | `glpat-` の personal access token |
| `SECRET_SLACK_TOKEN` | error | `xoxa-`、`xoxb-`、`xoxp-`、`xoxr-`、`xoxs-`、`xapp-` トークン |
| `SECRET_STRIPE_SECRET_KEY` | error | `sk_live_` / `sk_test_` secret key、`rk_live_` / `rk_test_` restricted key |
| `SECRET_OPENAI_API_KEY` | error | `sk-proj-`、`sk-svcacct-`、`sk-admin-` API キー |
| `SECRET_GOOGLE_API_KEY` | error | `AIza…` の API キー |
| `SECRET_URL_CREDENTIALS` | error | ユーザー名とプレースホルダーではない password を埋め込んだ URL |
| `SECRET_ENVIRONMENT_VALUE` | error | テンプレート以外のコミット済み `.env` で、機密名の変数にリテラル値がある |
| `SECRET_NPM_TOKEN` | error | `.npmrc` の `_authToken` に直接書かれた値（`${VAR}` 参照は許可） |
| `SECRET_NETRC_PASSWORD` | error | `.netrc` に直接書かれた `password` |
| `CREDENTIAL_FILE_IN_DEPLOY_CONTEXT` | error | `id_rsa` / `id_dsa` / `id_ecdsa` / `id_ed25519` の秘密鍵ファイル |
| `SENSITIVE_KEYSTORE_IN_DEPLOY_CONTEXT` | warning | `.p12`、`.pfx`、`.key` ファイル |
| `SECRET_FINDINGS_TRUNCATED` | warning | 検出件数の上限に達し、結果が不完全である可能性 |
| `ENVIRONMENT_VARIABLE_UNDOCUMENTED` | warning（`--strict` で error） | `process.env.X` / `import.meta.env.X` / `Deno.env.get("X")` / `Bun.env.X` が `.env.example` などやプロバイダー設定で宣言されていない |
| `SENSITIVE_ENVIRONMENT_EXPOSED_TO_CLIENT` | error | 機密名に public prefix が付いている |

public prefix として認識するのは `NEXT_PUBLIC_`、`VITE_`、`REACT_APP_`、`NUXT_PUBLIC_`、`GATSBY_`、`EXPO_PUBLIC_`、`VUE_APP_`、`PUBLIC_`、`ASTRO_PUBLIC_` です。これらの prefix が付いた名前でも、`PASSWORD`、`TOKEN`、`SECRET`、`PRIVATE_KEY`、`API_KEY`、`DATABASE_URL` などを含む場合は機密として扱います。

文書化済みかどうかは `.env.example`、`.env.sample`、`.env.template`、`.env.defaults` と、対応プロバイダー設定の非 secret 宣言で判定します。設定済みかどうかはプロバイダー設定（`render.yaml` の `envVars`、`vercel.json` の `env`、`wrangler` の `vars`）で判定します。プロバイダーが提供する実行時変数（`PATH`、`NODE_ENV`、`CI`、`VERCEL_*` など）は認識され、未文書化としては報告しません。

## 重要度

- **error** — コマンドを失敗させます（終了コード 1）。`--strict` でも変わりません。
- **warning** — 報告しますが、`--strict` を指定しない限りコマンドは失敗しません。
- **info** — 参考情報で、結果には影響しません。

## 関連ドキュメント

- [CLI リファレンス](cli.ja.md) — コマンド、オプション、終了コード
- [ドキュメント](README.ja.md) — 検査範囲と限界
- [リポジトリ構成](architecture.ja.md) — アダプターの実装場所と追加方法

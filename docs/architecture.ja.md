# リポジトリ構成

[English](architecture.md) | **日本語**

← [ドキュメント](README.ja.md)

リポジトリの構成、コマンドがコード内をどう流れるか、プロバイダーや検査項目を追加する場所をまとめます。

## トップレベル

```
dotdeployer/
├── bin/dotdeployer.js        実行ファイル（src/cli を読み込み、終了コードを設定）
├── docs/                     ドキュメント（英語 / 日本語。詳細は後述）
├── src/                      実装（詳細は後述）
├── test/                     node:test のテスト。src/ と対応した配置
├── .github/workflows/        CI: テストと strict なセルフ監査
├── package.json              依存、engines（Node 20 以上）、bin の対応
├── package-lock.json         npm ci が使う lockfile
├── LICENSE                   MIT
└── README.md                 英語の短い入口ページ（docs/ へのリンク）
```

`test/` はソースツリーと対応しています。

| テスト | 対象 |
|---|---|
| `test/cli/args.test.js` / `test/cli/cli.test.js` | オプション解析、コマンド別ヘルプ、終了コード、JSON CLI 契約 |
| `test/commands/audit.test.js` | secret ルール、環境変数契約、SARIF 出力 |
| `test/commands/deployment.test.js` | `verify` / `deploy` のゲート、CLI 確認、サイズ検査 |
| `test/commands/emulation.test.js` | 公式 CLI による emulation の経路と失敗時の挙動 |
| `test/project/files.test.js` | デプロイ対象の走査と一時コピー |
| `test/project/preflight.test.js` | プロバイダーごとの `checkProject` の挙動、ビルド、20 MB 上限 |

`docs/` はトピックごとに英語 / 日本語の 2 ファイルを 1 組で持ちます。更新は必ず同時に行ってください。

| 英語 | 日本語 |
|---|---|
| [`README.md`](README.md) | [`README.ja.md`](README.ja.md) |
| [`cli.md`](cli.md) | [`cli.ja.md`](cli.ja.md) |
| [`providers.md`](providers.md) | [`providers.ja.md`](providers.ja.md) |
| [`architecture.md`](architecture.md) | [`architecture.ja.md`](architecture.ja.md) |

## `src/`

```
src/
├── cli/
│   ├── args.js           CLI 引数の解析とオプション検証
│   ├── help.js           全体ヘルプとコマンド別ヘルプ
│   ├── index.js          CLI の実行制御と終了コード
│   ├── output.js         人間向け / JSON / provider レポートの整形
│   └── spec.js           コマンドとオプション定義の単一の情報源
├── commands/
│   ├── check.js          静的 preflight: ファイル走査 → 共通検査 + プロバイダー検査
│   ├── audit.js          secret パターンと環境変数契約
│   ├── build.js          一時コピー、依存インストール、ビルドコマンドの決定
│   ├── emulation.js      公式 CLI によるプロバイダーのビルド / 検証
│   └── deployment.js     verify（CLI 認証確認）と deploy（ゲート付き Workers 公開）
├── core/
│   ├── constants.js      バージョン、プロバイダー登録、20 MB 上限、無視するディレクトリ
│   ├── errors.js         DotdeployerError、issue()、summarizeIssues()
│   └── output.js         ビルド生成物のサイズ検査
├── project/
│   ├── files.js          デプロイ対象の列挙とファイル単位のガード
│   ├── config.js         YAML / TOML / JSON / JSONC の解析と構造チェック
│   └── source.js         ローカルディレクトリ、または GitHub の一時 clone
├── providers/index.js    プロバイダーの検出とプロバイダーごとの静的バリデータ
├── reporting/sarif.js    SARIF 2.1.0 への変換
└── mcp/server.js         読み取り専用ツールを公開する MCP stdio サーバー
```

各層の役割は次のとおりです。

- **`cli/`** はコマンド解析、通常の stdout / stderr 出力、コマンド結果コードを担当します。`bin/dotdeployer.js` は返されたコードを適用し、未捕捉の起動エラーを処理します。CLI 層に検証ロジックは置きません。
- **`commands/`** はユーザー向けコマンドを 1 つずつ実装します。ここにある関数は MCP サーバーからも API として呼ばれるため、コンソール出力を含めないでください。
- **`core/`** はプロバイダーやコマンドを知らないプリミティブを置きます。
- **`project/`** は「プロジェクトディレクトリまたは GitHub リポジトリ」を、デプロイ対象ファイルの一覧と解析済み設定に変換する役割を持ちます。
- **`providers/`** は純粋な静的検証で、サブプロセスもネットワークも使いません。
- **`reporting/`** は既存のレポートを別形式に変換します。

## コマンドの流れ

1. `bin/dotdeployer.js` が `src/cli/index.js` の `main()` を呼びます。
2. `src/cli/args.js` の `parseArgs()` が `src/cli/spec.js` の定義を使ってコマンド別オプションを検証し、`src/cli/help.js` が全体 / コマンド別ヘルプを生成します。使用法エラーは終了コード 2 です。
3. `withProjectSource()`（`src/project/source.js`）が対象を解決します。ローカルディレクトリか、`--repo` 指定時の一時 clone です。一時 clone は処理後に削除します。
4. `scanProjectFiles()`（`src/project/files.js`）がデプロイ対象を列挙します。Git リポジトリでは未追跡ファイルを含む `git ls-files`、それ以外ではフィルタ付きのディレクトリ走査です。プロジェクト外へ出るリンクを拒否し、20 MB に達するファイルがあれば即座に中止します。これは**設定を解析する前**に実行されます。
5. `checkProject()`（`src/commands/check.js`）が `validateGenericProject()` を実行し、続けて `src/providers/index.js` の選択されたアダプターを、`src/project/config.js` の `parseConfigFile()` を使って実行します。検出結果は `issue()` で集め、`summarizeIssues()` で集計します。
6. コマンドに応じて、この preflight の上に次のいずれかが動きます。
   - `auditProject()` — secret ルール、ファイル名ルール、環境変数契約
   - `runLocalBuild()` — デプロイ対象を一時ディレクトリへコピーし、必要なら lockfile から依存を取得し、ビルドコマンドを 1 つ決定して実行し、生成物を `src/core/output.js` で再検査
   - `emulateProject()` — プロバイダーの公式 CLI を実行し、`emulationLevel` と `limitations` を記録
   - `verifyProject()` / `deployProject()` — CLI の認証確認、環境の dry-run、そして `--execute --yes` がある場合のみ実際の publish
7. `src/cli/output.js` がレポート（人間向けテキストまたは `--json`）を整形し、SARIF は `src/reporting/sarif.js` 経由でシリアライズします。`src/cli/index.js` は handler を選択して終了コードを返します。

## 不変条件

コードベース全体で守っている前提です。機能を追加するときも維持してください。

1. **secret の値は出力に到達しません。** ルールが返すのはファイル、行、ルール ID、修正案だけで、テキスト・JSON・SARIF のいずれにも一致値が含まれないことをテストで検証しています。
2. **プロバイダーの identity は出力に到達しません。** `verify` と `deploy` は whoami の出力を抑制し、`{ checked, authenticated, identityOutputSuppressed }` だけを返します。
3. **サイズガードが最初に動きます。** デプロイ対象がファイル単位の上限を通過するまで、設定の解析もサブプロセスの起動もしません。違反時は `stoppedBeforeConfigValidation` が立ちます。
4. **パスはプロジェクト外へ出ません。** 設定からの参照、シンボリックリンクの解決先、ビルド出力ディレクトリ、Docker コンテキストをすべて解決して検査します。
5. **`check` と `audit` はオフラインで副作用がありません。** サブプロセスを起動するのは `test --build/--install`、`emulate`、`verify`、`deploy` だけで、リモートへの書き込みは `deploy --execute --yes` のみです。
6. **検出結果が機械可読な契約です。** issue コードは安定した識別子で、重要度は `error` / `warning` / `info` の 3 段階です。`--strict` は検査層で warning を error に格上げするため、利用側が受け取るレポートの形は常に同じです。
7. **設定は CLI フラグのみで行います。** dotdeployer 自身の設定ファイルはなく、検査の挙動を変える環境変数もありません。

## プロバイダーを追加する

1. `src/core/constants.js` の `PROVIDERS` に項目（id、label、description、`configNames`）を追加します。検出はデプロイ対象内の任意の場所でファイル名を照合するため、名前は正確かつ網羅的にしてください。
2. `src/providers/index.js` にバリデータを書き、`validateProvider()` に登録します。解析・構造・パスのエラーをプロバイダー間で揃えるため、`parseConfigFile()`、`assertObjectConfig()`、`validateString()`、`addTypeIssue()`、`validatePathExists()` を使ってください。
3. 各検出にはプロバイダー名を接頭辞にした安定したコード（例: `RAILWAY_HEALTHCHECK_PATH_INVALID`）、重要度、必要なら修正案を付けます。
4. 公式のローカル CLI がある場合は `src/commands/emulation.js` に分岐を追加し、ローカル実行がどこまで踏み込むかを `emulationLevel` で正確に表現します。GAS のように公式 CLI がない場合は、近似せず失敗させます。
5. 認証確認のコマンドがある場合は `src/commands/deployment.js` の `ACCESS_PROBES` に追加します。
6. [providers.ja.md](providers.ja.md) と [providers.md](providers.md) を更新し、`test/` にテストを追加します。

## 検査項目を追加する

1. `src/core/errors.js` の `issue(severity, CODE, message, file?, remediation?)` で検出を返します。コマンドモジュール内で出力しないでください。
2. `error` は明確に壊れているものだけに使います。時間とともに変わるプロバイダーの挙動に依存するものは `warning` または `info` にし、`--strict` の意味を保ちます。
3. SARIF が位置情報を出せるよう、`file`（テキストの検出では `line` も）を付けてください。
4. コードと重要度の両方を検証するテストを追加します。`--strict` での格上げも対象に含めてください。

## ドキュメントのルール

- 英語の各ドキュメントには、同じ構成・同じ表を持つ日本語版があります。1 回の変更で両方を更新してください。
- すべてのドキュメントは言語切り替えリンクとドキュメント一覧へのリンクを先頭に置きます。
- コマンドの構文、オプション名、検出コードはツールのインターフェースの一部なので、そのままの表記で書きます。

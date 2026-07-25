# Galileo の Project と Log stream

collector と `hermes-galileo` の送信先を、用途ごとに分けて管理します。

## 目的

一つの Galileo Project に Agent 実行を集めつつ、Log stream で送信元や環境を区別できる状態を作ります。

Project と Log stream を分けると、Galileo 上で保持期間、評価設定、アクセス方針を surface ごとに判断できます。

## できること

collector を使う Codex と Claude Code は、次の route ごとに既定の Log stream を持ちます。

| collector route | 既定の Log stream | 利用する surface |
| --- | --- | --- |
| `codex` | `codex` | Codex Desktop |
| `codex-cli` | `codex-cli` | Codex CLI |
| `claude-code` | `claude-code` | Claude Desktop の Local session |
| `claude-code-cli` | `claude-code-cli` | Claude Code CLI |
| `chatgpt` | `chatgpt` | 将来の公式 OTLP 設定面のための予約 route |
| `claude-cowork` | `claude-cowork` | 変換 fixture と将来の接続方式のための予約 route |

Hermes Agent には `hermes-agent` のような専用 Log stream を作れます。

ただし Hermes は collector route を使わず、[hermes-galileo](08-hermes-agent.md) の `GALILEO_LOG_STREAM` で直接指定します。

## 最小の設定例

Galileo Cloud で Project を一つ作り、collector 専用の API key を発行します。

表示された API key はパスワードマネージャーへ保存し、Git、shell history、スクリーンショット、Agent 設定には残しません。

collector では API key と Project だけが必須です。

```dotenv
GALILEO_API_KEY=実際のAPIキー
GALILEO_PROJECT=galileo-allsky
```

Log stream を既定名から変える場合だけ、collector 用の `collector.env` に route を追加します。

```dotenv
GALILEO_CODEX_LOG_STREAM=codex
GALILEO_CODEX_CLI_LOG_STREAM=codex-cli
GALILEO_CLAUDE_CODE_LOG_STREAM=claude-code
GALILEO_CLAUDE_CODE_CLI_LOG_STREAM=claude-code-cli
```

同じ Log stream にまとめる場合は、複数の route に同じ値を設定できます。

設定後は、API key を表示せずに構成を検査します。

```sh
/path/to/galileo-allsky/.venv/bin/allsky-collector \
  --env-file ~/.config/galileo-allsky/collector.env \
  --check-config
```

`routes` と `project` が意図した値で表示されれば、collector の送信先は固定されています。

## 送信先の仕組み

collector は binary protobuf の `ExportTraceServiceRequest` を、既定で `https://api.galileo.ai/otel/v1/traces` へ直接 POST します。

送信元の Agent request に Galileo 用 header や `galileo.*` 属性があっても、collector は送信先に採用しません。

collector が送る header は次の五つです。

| Header | 値の取得元 |
| --- | --- |
| `Content-Type` | 固定値 `application/x-protobuf` |
| `Galileo-API-Key` | `GALILEO_API_KEY` |
| `project` | `GALILEO_PROJECT` |
| `logstream` | collector route に対応する Log stream |
| `User-Agent` | `galileo-allsky/0.1.0` |

## 制約と安全な運用

`GALILEO_OTLP_TRACES_ENDPOINT` は任意の完全な upstream URL です。

通常運用では HTTPS URL を使います。

HTTP URL は trusted loopback Galileo stub を使う test のためだけに `ALLSKY_ALLOW_INSECURE_UPSTREAM=true` と組み合わせられます。

collector の dotenv loader は、すでに process 環境にある変数を上書きしません。

Hermes 用の `GALILEO_API_KEY`、`GALILEO_PROJECT`、`GALILEO_LOG_STREAM` を collector の service 環境へ export せず、`collector.env` と `$HERMES_HOME/.env` を別 process の設定として分離します。

`chatgpt` と `claude-cowork` の route は collector に存在しますが、現行の local loopback 構成で通常の ChatGPT チャットと Cowork を接続する手順はありません。

## 公式資料

- [Galileo Quickstart](https://docs.galileo.ai/getting-started/quickstart)
- [API key、Project、Log stream の取得](https://docs.galileo.ai/references/faqs/find-keys)
- [Galileo OpenTelemetry Integration Recommendations](https://docs.galileo.ai/sdk-api/third-party-integrations/opentelemetry-and-openinference/integration-recommendations)

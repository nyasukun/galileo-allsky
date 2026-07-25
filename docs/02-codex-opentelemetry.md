# Codex の設定

## 目的

Codex の実行ログを collector へ送り、Galileo で会話単位の Agent graph として確認します。

## できること

Codex Desktop は macOS、Codex CLI は macOS と Ubuntu で collector に接続できます。

Codex の logs は user prompt、API event、tool decision、tool result などを含み、collector は会話 ID から Agent、LLM、Tool の親子関係を作ります。

Codex の native traces も受信できますが、一会話で多数の小さな trace に分かれるため、collector は正常応答だけを返し、Galileo への転送には logs から作る会話単位の graph を使います。

この設定は 2026-07-25 に公式の Codex telemetry と configuration reference で確認した内容です。

Codex を更新したときは、設定構文の検査と[実機受入](04-coverage-and-verification.md)を再実行します。

## 最初に確認する条件

この設定は、collector と同じ host かつ同じ network namespace で実行する Codex Desktop または CLI 向けです。

通常の ChatGPT チャット、ChatGPT Classic、音声会話には利用者が設定できる公式 OTLP exporter がないため、この手順は使えません。

## 最初に設定する場所

telemetry routing はユーザー設定 `~/.codex/config.toml` に置きます。

プロジェクト配下の `.codex/config.toml` にある `[otel]` は telemetry routing に使われません。

Ubuntu で Codex の sandbox を使う場合は、必要に応じて公式の [Linux sandbox 前提条件](https://learn.chatgpt.com/docs/sandboxing#prerequisites)に従って `bubblewrap` を導入します。

## Codex Desktop の例

macOS の `~/.codex/config.toml` に、既存の `[otel]` table と統合して次の値を設定します。

```toml
[otel]
environment = "local-galileo"
log_user_prompt = false
metrics_exporter = "none"
exporter = { otlp-http = { endpoint = "http://127.0.0.1:4318/v1/logs", protocol = "binary", headers = { "X-Allsky-Agent" = "codex" } } }
trace_exporter = { otlp-http = { endpoint = "http://127.0.0.1:4318/v1/traces", protocol = "binary", headers = { "X-Allsky-Agent" = "codex" } } }
```

設定後は Codex Desktop を完全に終了し、新しい task を開始します。

`metrics_exporter = "none"` は、metrics を受けない collector へ metrics を送らないための設定です。

## Codex CLI の例

Desktop と CLI を別の Log stream へ分ける場合は、`~/.codex/galileo-cli.config.toml` を作ります。

```toml
[otel]
environment = "local-galileo"
log_user_prompt = false
metrics_exporter = "none"
exporter = { otlp-http = { endpoint = "http://127.0.0.1:4318/v1/logs", protocol = "binary", headers = { "X-Allsky-Agent" = "codex-cli" } } }
trace_exporter = { otlp-http = { endpoint = "http://127.0.0.1:4318/v1/traces", protocol = "binary", headers = { "X-Allsky-Agent" = "codex-cli" } } }
```

macOS または Ubuntu で、profile を指定して CLI を起動します。

```sh
codex --profile galileo-cli
codex exec --profile galileo-cli "実行するタスク"
```

CLI と Desktop を同じ Log stream にまとめる場合は、profile を作らず、両方で `X-Allsky-Agent = "codex"` を使えます。

設定構文は次のコマンドで確認できます。

```sh
codex --strict-config --version
codex --profile galileo-cli --strict-config --version
```

collector の `/status` と Galileo の Log stream に到着が見えれば、接続は完了です。

## 本文を取得する場合

prompt 本文を保存する場合だけ、対象の `[otel]` table で次を有効にします。

```toml
[otel]
log_user_prompt = true
```

collector 側でも `ALLSKY_CAPTURE_CONTENT=true` が必要です。

設定範囲、確認、停止は[本文を Galileo へ転送する設定](07-content-capture.md)に従います。

## 制約

Codex が出さない assistant の最終回答本文を collector が生成することはありません。

通常の ChatGPT チャット、ChatGPT Classic、音声会話には、利用者が設定できる公式 OTLP exporter がないため対象外です。

ChatGPT Desktop 内で実行する Codex task は、Codex の exporter が設定されている場合に `codex` route として記録できます。

`X-Allsky-Agent` は設定例では必ず指定します。

header を省略した request は resource 属性から送信元を解決できる場合がありますが、複数 source の混入を避けるため運用設定では header を固定します。

## 公式資料

- [Codex の Observability and telemetry](https://learn.chatgpt.com/docs/config-file/config-advanced#observability-and-telemetry)
- [Codex Configuration Reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- [Codex sandbox の Linux 前提条件](https://learn.chatgpt.com/docs/sandboxing#prerequisites)

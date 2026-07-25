# 対応範囲と検証方針

この文書は、どの Agent をどの経路で接続でき、何を自動テストと実機受入で確認するかを示します。

## 目的

collector の変換契約と、実際の Agent 接続で確認すべき範囲を分けます。

自動テストが成功すれば、OTLP fixture から Galileo stub までの変換、privacy、routing、失敗応答の契約を再現できます。

実 Agent の製品更新や Galileo Cloud への永続化は、別に短い実機受入で確認します。

## 接続できる surface

| surface | 導入ガイドの OS | 接続手順 | collector が作る結果 | 自動 fixture の範囲 |
| --- | --- | --- | --- | --- |
| Codex Desktop | macOS | [あり](02-codex-opentelemetry.md) | logs から batch 単位の Agent graph | Codex logs と trace 抑止 |
| Codex CLI | macOS、Ubuntu | [あり](02-codex-opentelemetry.md) | logs から batch 単位の Agent graph | Codex logs と trace 抑止 |
| Claude Desktop の Local session | macOS、Ubuntu beta | [あり](03-claude-code-opentelemetry.md) | trace hierarchy と event span | Claude の主要 logs と traces |
| Claude Code CLI | macOS、Ubuntu | [あり](03-claude-code-opentelemetry.md) | trace hierarchy と event span | Claude の主要 logs と traces |
| Hermes Agent | macOS、Ubuntu | [hermes-galileo](08-hermes-agent.md) | plugin が Galileo に直接 trace と native Session を送信 | `hermes-galileo` の plugin contract は Ubuntu CI。macOS は実機受入が必要 |

表の OS は、このプロジェクトが導入手順を提供する範囲です。

製品ベンダーによる全実行形態のサポート、またはその OS での実機接続済みを意味しません。

`hermes-galileo` は collector を通らない別経路です。

Hermes の observer hook、native Session、direct SDK の契約は [hermes-galileo](https://github.com/nyasukun/hermes-galileo) 側の test と運用受入で確認します。

## collector が検証すること

自動テストは、次の成功条件を検証します。

- OTLP/HTTP protobuf の `/v1/logs` と `/v1/traces` を受ける。
- `Content-Length` と上限付き chunked request、gzip request を安全に受ける。
- allowlist の route だけを固定した Log stream へ送る。
- Agent request の Galileo routing 属性で送信先を上書きできない。
- 転送対象の log record を一つの span へ変換する。
- Codex の同一 OTLP batch を Agent root と子 span にまとめ、会話 ID は batch 間で同じ仮名属性にする。
- Claude Code の既存 trace ID、parent ID、時刻を保ち、不正値だけを補う。
- Codex native traces を正常応答で抑止し、Galileo へ重複送信しない。
- prompt、response、tool payload を既定で送らず、secret、reasoning、raw identity を upstream protobuf に残さない。
- Galileo の JSON と protobuf の成功応答、partial success、retryable HTTP error を Agent 側の OTLP 応答へ変換する。

Codex で会話を確定できない一部の log は、誤った会話へ結合しないため意図的に抑止します。

Claude の hook と subagent に由来する詳細な span は、受信して一般 Agent span として扱えますが、fixture では主要 hierarchy だけを分類確認しています。

## テストの実行例

macOS または Ubuntu の導入先で、次の suite を実行します。

```sh
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest
```

loopback socket の bind が禁止された sandbox では integration test を実行できません。

その場合は test を skip せず、loopback を許可した隔離環境で実行します。

fixture には canary secret、email、session ID を含め、Galileo stub が受けた serialized protobuf に残らないことを確認します。

## 実機受入の例

collector を起動した後、接続対象の surface ごとに秘密を含まない短い task を一度だけ実行します。

```sh
curl --fail http://127.0.0.1:4318/status
```

実行前後の `/status` を比較し、対象 route の counter が増えたことを確認します。Codex CLI なら `agent.codex.requests` または `agent.codex-cli.requests`、Claude Code CLI なら `agent.claude-code-cli.requests` が対象です。

別 Agent の counter だけが増えた場合は到着成功として扱いません。起動環境の `OTEL_EXPORTER_OTLP_HEADERS` が別 route の `X-Allsky-Agent` を上書きしていないか確認します。

`last_success_at`、実行 surface、製品 version、日時、Log stream、到着の有無、collector error type を記録します。

API key、prompt 本文、tool output、HMAC 値は記録しません。

Galileo の Log stream で trace の到着を確認して初めて、実機接続の受入を完了とします。

## 対象外と未保証

通常の ChatGPT チャットは利用者向け OTLP exporter がないため対象外です。

Claude Cowork は Team または Enterprise の OTel monitoring を使える場合でも、Cowork VM から local loopback collector へ到達できないため実接続の対象外です。

Cloud session、SSH session、remote host、container、VM のように collector と network namespace を共有しない実行も対象外です。

v0.1 は OTLP metrics、OTLP/JSON、OTLP/gRPC、durable spool、crash recovery、tail sampling、non-loopback listener、receiver authentication を提供しません。

製品更新後の exporter schema と Galileo Cloud の保存結果は自動 fixture だけでは証明できないため、更新時は同じ実機受入を再実行します。

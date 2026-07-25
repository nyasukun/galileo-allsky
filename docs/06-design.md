# collector の変換設計

collector は、Agent が送る OTLP を Galileo が保存できる Agent、LLM、Tool trace へ正規化します。

## 目的

異なる Agent の telemetry を、Galileo で同じ観点から比較できる trace にします。

転送する span には `openinference.span.kind`、GenAI の操作種別、input、output を与えます。

本文を取得しない場合も、input と output には `[content capture disabled]` を入れ、実内容を送らずに trace の構造を維持します。

## 得られる trace

Codex の logs は同じ OTLP batch 内で一つの Agent root とその子 span にまとめます。

同じ会話が複数 batch に分かれた場合は batch ごとに別 trace ID を使い、仮名化した `gen_ai.conversation.id` で相関できるようにします。

Claude Code の traces は、trace ID、span ID、parent ID、時刻、親子関係を維持しながら Galileo 向けに補正します。

| 入力 | 出力 | 代表的な属性 |
| --- | --- | --- |
| Agent event | `invoke_agent` | `openinference.span.kind=AGENT`、`gen_ai.agent.name` |
| LLM event または span | `chat` | `openinference.span.kind=LLM`、model がある場合の `gen_ai.request.model` |
| Tool event または span | `execute_tool` | `openinference.span.kind=TOOL`、入力にある場合の tool name と call ID |

`gen_ai.provider.name`、legacy の `gen_ai.system`、LLM の `llm.system` も source の provider から設定します。

## 変換の例

Codex の一つの OTLP batch に `user_prompt`、API event、tool result がある場合、collector は最初の Agent span を root とし、後続の LLM と Tool span をその子にします。

Codex native trace は一会話で多数の trace ID に分かれるため、正常な OTLP 応答を返すだけで Galileo へは転送しません。

Claude Code の `claude_code.interaction`、`claude_code.llm_request`、`claude_code.tool` は、元の hierarchy を保ったまま Agent、LLM、Tool として送ります。

`tool.output` span event がある場合は、content capture が有効なときだけ、秘密情報を除去して親 Tool span の output に反映します。

このため、Galileo では同じ batch に入った Agent の実行、モデル呼び出し、tool 実行を同じ trace でたどれます。

## source の解決と logs の相関

`X-Allsky-Agent` がある request は、その値を送信元として扱います。

header がない場合は、`allsky.agent`、`agent.surface`、`service.name` の resource 属性が同じ supported source を示す request だけを受けます。

logs に有効な trace ID がない場合は、`conversation.id`、`gen_ai.conversation.id`、`session.id`、必要に応じて `prompt.id` を使い、同じ batch の record に synthetic trace ID を付けます。

同じ会話の後続 batch には別の synthetic trace ID を付けます。

Galileo の direct OTLP endpoint へ送信済みの trace ID を再利用すると、後続 request が成功応答でも span が追記されない場合があるためです。

raw の会話識別子は HMAC の入力にだけ使い、属性や trace ID として転送しません。

Codex の同一 scope batch に一つの会話 ID だけがある場合は、ID が省略された record にも補完します。

会話を確定できない Codex 固有 trace ID だけの log は、誤った会話へ結合しないため抑止します。

したがって、一対一変換は Galileo へ転送する対象 log record に適用される契約です。

## 信頼境界と privacy

Agent request は送信元 ID 以外の Galileo routing を指定できません。

`galileo.experiment.id`、`galileo.dataset.*`、`galileo.project.*`、`galileo.logstream.*` は入力から除去し、collector の信頼済み設定で resource 属性と upstream header を設定し直します。

content capture の既定値は false です。

prompt、response、messages、body、tool arguments、tool result、command、file content は送信前に sentinel へ置き換えます。

capture を有効にした場合も、API key、authorization、cookie、password、credential、private key、JWT、cloud access key、reasoning、thinking、chain of thought、thought signature は送信しません。

session、conversation、user、account、email、organization、host、workspace host path は HMAC-SHA256 の仮名へ変換します。

`ALLSKY_PSEUDONYM_SECRET` を専用値にすると、API key rotation と仮名の安定性を分離できます。

## 応答と配送

正常時は、Agent に signal に対応する binary protobuf の `ExportLogsServiceResponse` または `ExportTraceServiceResponse` を返します。

request や upstream のエラー時は、binary protobuf の `google.rpc.Status` を返します。

Galileo の空 body、JSON protobuf response、binary protobuf response を受け、partial success は対応する Agent signal の partial success に変換します。

collector は一回の upstream attempt の後、retryable HTTP status と `Retry-After` を Agent exporter へ返します。

request は圧縮前と gzip 展開後の byte 数、item 数、正規化後の output byte 数を上限で制限します。

## 既知の限界

collector は source に存在しない本文を生成しません。

Codex の assistant 最終回答、通常の ChatGPT chat の telemetry、current Cowork の local loopback 接続は補えません。

v0.1 は memory queue、disk spool、durable delivery を持ちません。

Hermes Agent はこの変換層を使わず、[hermes-galileo](08-hermes-agent.md) が直接 Galileo SDK へ送ります。

## 参照仕様

- [Galileo OpenTelemetry Integration Recommendations](https://docs.galileo.ai/sdk-api/third-party-integrations/opentelemetry-and-openinference/integration-recommendations)
- [OpenTelemetry Protocol Specification](https://opentelemetry.io/docs/specs/otlp/)
- [OpenInference Semantic Conventions](https://arize-ai.github.io/openinference/spec/semantic_conventions.html)

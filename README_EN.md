# workbuddy-desktop-api

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-teal)](https://fastapi.tiangolo.com/)

Turn your local **WorkBuddy Desktop login session** into an **OpenAI + Anthropic compatible API**.

Supports `/v1/chat/completions`, `/v1/responses` and `/v1/messages`, with native tool calling, reasoning and multi-account load balancing.

Models come from the WorkBuddy cloud account list (`auto`, `hy3`, `hy3-x`, `hy4-preview`, `hy4-preview-f`, `fast-model`, `balanced-model`, `deep-model`, `glm-5.3`, `kimi-k2.6`, `deepseek-v4-pro`, ...) and are discovered dynamically.

Forked from [xiaomi-mimo-desktop-api](https://github.com/Fly143/xiaomi-mimo-desktop-api): same protocol layer, upstream switched from MiMo Desktop to WorkBuddy Desktop.

## How it works

The app reads the Desktop credential file written by WorkBuddy:

| OS | Path |
|----|------|
| Windows | `%LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\workbuddy-desktop.info` |
| macOS | `~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info` |
| Linux | `~/.local/share/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info` |

It then calls `https://copilot.tencent.com/v2/chat/completions` with `Authorization: Bearer <accessToken>`, refreshing the token via `/v2/plugin/auth/token/refresh` when needed.

> The upstream only accepts `stream=true`; non-streaming responses are aggregated locally.

> 📖 中文文档见 [README.md](README.md)

## Features

- OpenAI `/v1/chat/completions`, `/v1/models`
- Anthropic `/v1/messages` (+ count_tokens, batches)
- Responses API `/v1/responses*`
- Function calling + stream sieve
- Multi-account rotation
- `passToken` → SSO → `serviceToken` (30 min cache, 401 retry)
- Fernet-encrypted credentials (`config.json` + `.secret_key`)

No TTS / ASR on this upstream.

## Quick start

```bash
pip install -r requirements.txt
# Sign in to WorkBuddy Desktop once, then quit Desktop (cookie DB lock)
python main.py
# http://127.0.0.1:8080  (binds 0.0.0.0 by default; set HOST=127.0.0.1 for local-only)
```

Admin UI `/` — Basic auth `admin` / `admin_password` → **Detect → Import**.

```bash
curl -u admin:change-me http://127.0.0.1:8080/api/desktop/auto-import
curl -u admin:change-me -X POST http://127.0.0.1:8080/api/desktop/import \
  -H "Content-Type: application/json" \
  -d '{"wbAccessToken":"…","wbUid":"…","uid":"…"}'
```

## Call

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-workbuddy" \
  -H "Content-Type: application/json" \
  -d '{"model":"hy4-preview","messages":[{"role":"user","content":"hi"}]}'
```

Claude aliases on `/v1/messages` (see `ANTHROPIC_MODEL_ALIASES`):

| Claude name | → WorkBuddy model |
|---|---|
| `claude-opus-4-7` / `4-6` / `4-5` / `4-1` / `4-0` / `claude-3-opus` | `hy4-preview` |
| `claude-sonnet-*` / `claude-haiku-*` / `claude-3-*-sonnet` / `claude-3-haiku` | `hy3` |
| `*-search` / `*-thinking` / `*-nothinking` variants | same tier as base name |

Also strips `-YYYYMMDD` / `-YYYY-MM-DD` / `-latest` before lookup; unknown `claude-*`: `opus` → `hy4-preview`, else `hy3`. Native model ids (e.g. `hy4-preview`, `hy3`, `auto`) pass through unchanged.

## Environment

| Var | Default |
|-----|---------|
| `HOST` | `0.0.0.0` |
| `PORT` | `8080` |

## Security

- Back up `config.json` **and** `.secret_key` together
- Change `admin_password` / `api_keys` before exposing the port
- `passToken` is account login state — treat it as a secret

## License

MIT

# workbuddy-desktop-api

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-teal)](https://fastapi.tiangolo.com/)

把本机 **WorkBuddy Desktop 登录会话**转成 **OpenAI + Anthropic 兼容 API**：
`/v1/chat/completions`、`/v1/responses`、`/v1/messages` 三套协议全支持，含原生工具调用、深度思考、多账号负载均衡。

模型来自 WorkBuddy 云端账号清单（`auto` / `hy3` / `hy3-x` / `hy4-preview` / `hy4-preview-f` / `fast-model` / `balanced-model` / `deep-model` / `glm-5.3` / `kimi-k2.6` / `deepseek-v4-pro` …），随服务端配置动态拉取。

本项目由 [xiaomi-mimo-desktop-api](https://github.com/Fly143/xiaomi-mimo-desktop-api) 改造而来：保留协议与业务层，上游从小米 MiMo Desktop 换为 WorkBuddy Desktop。

> 📖 [English Version](README_EN.md)

## 目录

- [特性](#特性)
- [架构](#架构)
- [快速开始](#快速开始)
  - [一键部署](#一键部署)
  - [手动安装](#手动安装)
- [配置凭证](#配置凭证)
  - [方法1：管理页自动导入](#方法1管理页自动导入)
  - [方法2：API 导入](#方法2api-导入)
  - [多账号管理](#多账号管理)
  - [凭证加密存储](#凭证加密存储)
- [API 使用](#api-使用)
  - [列出模型](#1-列出模型)
  - [文本对话](#2-文本对话)
  - [流式对话](#3-流式对话)
  - [工具调用（Function Calling）](#4-工具调用function-calling)
  - [深度思考模式](#5-深度思考模式)
- [Anthropic Messages API](#anthropic-messages-api)
- [Responses API 详解](#responses-api-详解)
- [工具调用详解](#工具调用详解)
- [管理命令](#管理命令)
- [项目结构](#项目结构)
- [配置参考](#配置参考)
- [依赖](#依赖)
- [限制与已知问题](#限制与已知问题)
- [常见问题](#常见问题)
- [许可](#许可)

## 特性

- **OpenAI 完全兼容** — 标准 `/v1/chat/completions`（流式/非流式）、`/v1/models`、`/v1/models/{id}`，可直接对接 ChatBox、NextChat、LobeChat 等
- **Anthropic Messages API 兼容** — 完整支持 `/v1/messages`（流式/非流式）+ count_tokens + batches CRUD，可对接 RikkaHub、Claude Code 等
- **Responses API** — `/v1/responses*` 系列端点
- **Desktop 独占模型** — `hy4-preview`、`hy3`，走 Desktop 账号会话，不依赖网页 bot cookie
- **会话自动续期** — `passToken` 经 SSO 换 `serviceToken`，30 分钟缓存，401 自动重取重试
- **工具调用（Function Calling）** — 多策略提取（WorkBuddyML / <tool_call> / TOOL_CALL / JSON 等），自动清洗残留
- **流式筛分** — 有工具调用时实时分离正文与工具调用，客户端可逐步接收
- **深度思考** — 支持 `reasoning_effort`，自动分离 `<think>` 块
- **多账号池** — 多个 Desktop 账号轮询，降低单账号限频
- **上下文压缩** — 超长对话自动 compress / truncate
- **凭证加密** — Fernet 加密落盘（`enc:v1:`），密钥 `.secret_key`
- **CORS 全开** — 允许任意来源跨域访问

## 架构

```
┌──────────────────────────────────────────────────────────┐
│                     OpenAI / Anthropic 客户端               │
│            (ChatBox / LobeChat / Claude Code / curl)      │
└───────────────┬──────────────────────────────────────────┘
                │  /v1/chat/completions  |  /v1/messages
                ▼
┌──────────────────────────────────────────────────────────┐
│             workbuddy-desktop-api (FastAPI)               │
│  ┌─────────┐  ┌──────────────┐  ┌─────────────────────┐ │
│  │ routes  │  │ tool_sieve   │  │  workbuddy_client   │ │
│  │         │──│ (流式筛分)    │──│ (上游会话代理)        │ │
│  │anthropic│  │ tool_call    │  │  workbuddy_session  │ │
│  └─────────┘  └──────────────┘  └─────────────────────┘ │
└───────────────┬──────────────────────────────────────────┘
                │  Authorization: Bearer <accessToken>
                ▼
┌──────────────────────────────────────────────────────────┐
│     copilot.tencent.com                                  │
│     POST /v2/chat/completions   (OpenAI 兼容 SSE)         │
│     GET  /v3/config             (云端模型清单)             │
│     POST /v2/plugin/auth/token/refresh                   │
└──────────────────────────────────────────────────────────┘
```

会话链路：

```
本机凭证文件 workbuddy-desktop.info
  %LOCALAPPDATA%/CodeBuddyExtension/Data/Public/auth/
  → accessToken / refreshToken / uid / domain
  → 过期前自动 /v2/plugin/auth/token/refresh
  → 网关请求头（401/403 自动重试）

注：上游只接受 stream=true，非流式响应由本层聚合后返回。
```

## 快速开始

### 一键部署

```bash
git clone https://github.com/Fly143/workbuddy-desktop-api.git
cd workbuddy-desktop-api
chmod +x deploy.sh
./deploy.sh
```

### 手动安装

```bash
# 1. 克隆
git clone https://github.com/Fly143/workbuddy-desktop-api.git
cd workbuddy-desktop-api

# 2. 依赖
pip install -r requirements.txt

# 3. 配置（可选，也可启动后在管理页导入）
cp config.example.json config.json

# 4. 启动
python main.py
```

默认监听 **`0.0.0.0:8080`**（本机与局域网均可访问，例如 `http://192.168.x.x:8080`）。仅本机可用时设置 `HOST=127.0.0.1`。

### 管理后台

| 项 | 默认值 |
|----|--------|
| 地址 | http://127.0.0.1:8080 或 http://&lt;本机IP&gt;:8080 |
| 用户名 | `admin`（固定，不可改） |
| 密码 | `admin` （`config.json` → `admin_password`） |
| 调用 API Key | `sk-workbuddy` （`config.json` → `api_keys`） |

浏览器访问管理页时会弹出 **HTTP Basic** 登录框，输入 `admin` / `admin` 即可。curl 用 `-u admin:admin`。

**修改管理密码 / API Key：**

```bash
# 服务运行中，用当前密码调管理 API（只传要改的字段，不会清掉账号）
curl -u admin:admin -X POST http://127.0.0.1:8080/api/config \
  -H "Content-Type: application/json" \
  -d '{"admin_password":"你的新密码","api_keys":"sk-新key"}'
```

改完后 `config.json` 里会存成 `enc:v1:...` 密文，**不要再手改密文**。若服务未启动且配置尚未加密，也可直接编辑 `config.json` 里的明文 `admin_password` / `api_keys`。

**导入前请先登录一次 WorkBuddy Desktop。** Desktop 运行时会独占锁 cookie 数据库，导入失败时先退出 Desktop 再试。

### Docker

```bash
docker run -d -p 8080:8080 \
  -v $(pwd)/config.json:/app/config.json \
  -v $(pwd)/.secret_key:/app/.secret_key \
  ghcr.io/fly143/workbuddy-desktop-api:latest
```

`config.json` 与 `.secret_key` 需一起挂载。

## 配置凭证

### 方法1：管理页自动导入

1. 确保已登录 WorkBuddy Desktop（凭证文件无需退出客户端，直接读取）
2. 打开管理页 http://127.0.0.1:8080
3. HTTP Basic 登录：用户名 `admin`，默认密码 `admin`（见上文「管理后台」）
4. 点击 **自动检测** → **导入账号**

会读取本机 Desktop 凭证文件的 `accessToken` / `refreshToken` / `uid`。

凭证文件路径：

| 系统 | 路径 |
|------|------|
| Windows | `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info` |
| macOS | `~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info` |
| Linux | `~/.local/share/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info` |

> 服务首次启动且未配置账号时，会自动尝试导入本机会话，通常无需手动操作。

### 方法2：API 导入

```bash
# 探测本机凭证
curl -u admin:change-me http://127.0.0.1:8080/api/desktop/auto-import

# 导入
curl -u admin:change-me -X POST http://127.0.0.1:8080/api/desktop/import \
  -H "Content-Type: application/json" \
  -d '{
    "wbAccessToken": "…",
    "wbUid": "…",
    "wbRefreshToken": "…",
    "uid": "…"
  }'
```

### 多账号管理

- 多个 Desktop 账号轮询使用
- 支持测试连接、删除
- 同一 uid 重复导入会更新，不重复添加

### 凭证加密存储

`wb_access_token` / `wb_uid` / `wb_refresh_token` / `admin_password` 落盘时用 **Fernet** 加密（`enc:v1:` 前缀），密钥在同目录 **`.secret_key`**。

- 两个文件都已在 `.gitignore`
- 首次保存自动生成 `.secret_key`
- 旧明文 `config.json` 启动时自动迁移
- **备份时必须同时备份 `config.json` 和 `.secret_key`**

## API 使用

### 1. 列出模型

```bash
curl http://127.0.0.1:8080/v1/models \
  -H "Authorization: Bearer sk-workbuddy"
```

Desktop 通路固定返回：

| 模型 ID | 说明 |
|---------|------|
| `hy4-preview` | Desktop 独占 Pro Preview |
| `hy3` | Desktop 独占 Flash Preview |

### 2. 文本对话

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-workbuddy" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "hy4-preview",
    "messages": [
      {"role": "user", "content": "你好，请用中文回复"}
    ]
  }'
```

### 3. 流式对话

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-workbuddy" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "hy3",
    "messages": [
      {"role": "user", "content": "讲个故事"}
    ],
    "stream": true
  }'
```

返回标准 SSE 流（`data: ...\n\n`），以 `data: [DONE]\n\n` 结束。

### 4. 工具调用（Function Calling）

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-workbuddy" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "hy4-preview",
    "messages": [{"role": "user", "content": "北京今天天气怎么样？"}],
    "tools": [{
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "查询天气",
        "parameters": {
          "type": "object",
          "properties": {"city": {"type": "string"}},
          "required": ["city"]
        }
      }
    }]
  }'
```

流式时通过 StreamSieve 实时分离正文与 `tool_calls`。

### 5. 深度思考模式

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-workbuddy" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "hy4-preview",
    "messages": [{"role": "user", "content": "证明勾股定理"}],
    "reasoning_effort": "high"
  }'
```

思考内容在 `reasoning` / `reasoning_content` 字段，或 `<think>` 块中。

## Anthropic Messages API

```bash
curl http://127.0.0.1:8080/v1/messages \
  -H "x-api-key: sk-workbuddy" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-opus-4-6",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

Claude 模型名自动映射：

| Anthropic 名 | Desktop 模型 |
|--------------|--------------|
| `claude-opus-4-6` / `claude-opus-4-1` / `claude-3-opus` 等 opus 级 | `hy4-preview` |
| `claude-sonnet-*` / `claude-haiku-*` 等 | `hy3` |

支持端点：`/v1/messages`、`/v1/messages/count_tokens`、`/v1/messages/batches*` 等。

## Responses API 详解

见 `/v1/responses`、`/v1/responses/{id}`、`/v1/responses/{id}/input_items`、compact / cancel 等。内部基于 Chat Completions + 本地 `response_store` 持久化。

## 工具调用详解

提取策略覆盖 WorkBuddyML（`<|WorkBuddyML|tool_calls>`）、`<tool_call>`、`TOOL_CALL:`、JSON、`<function_call>` XML、中文「调用工具:」等；流式用 `StreamSieve` 实时切分；响应正文中的工具残留会自动清洗。

## 管理命令

```bash
# 后台启动
nohup python main.py > workbuddy.log 2>&1 &

# 停止
pkill -f 'python main.py'

# 管理页
open http://127.0.0.1:8080
```

常用管理 API（需 Basic `admin:password`）：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/accounts` | 账号列表（token 掩码） |
| POST | `/api/accounts/{idx}/test` | 测试连接 |
| DELETE | `/api/accounts/{idx}` | 删除账号 |
| GET | `/api/desktop/auto-import` | 自动检测本机 Desktop 凭证 |
| POST | `/api/desktop/import` | 导入 passToken |
| GET | `/api/config` / POST | 读写配置 |
| GET | `/api/usage` | 本地用量统计 |

## 项目结构

```
workbuddy-desktop-api/
├── main.py                  # 入口 + 启动预探测
├── deploy.sh
├── requirements.txt
├── config.example.json
├── config.json              # .gitignore，敏感字段已加密
├── .secret_key              # .gitignore，Fernet 密钥
├── Dockerfile
├── web/index.html           # 管理页（Desktop 自动导入）
└── app/
    ├── workbuddy_session.py   # passToken → SSO → serviceToken
    ├── auto_import.py       # 读本机 Desktop cookie 库
    ├── workbuddy_client.py       # copilot.tencent.com 客户端 + 旧接口适配
    ├── routes.py            # OpenAI / Responses / 管理
    ├── anthropic_routes.py  # Anthropic Messages
    ├── config.py            # 多账号 + Fernet 加密
    ├── tool_call.py         # 工具调用提取
    ├── tool_sieve.py        # 流式筛分
    ├── context_compressor.py
    ├── response_store.py
    ├── usage_store.py
    ├── batch.py
    ├── utils.py
    └── ...
```

## 配置参考

`config.json`：

```json
{
  "api_keys": "sk-workbuddy,sk-another",
  "admin_password": "enc:v1:gAAAAA...",
  "workbuddy_accounts": [
    {
      "wb_access_token": "enc:v1:gAAAAA...",
      "wb_uid": "enc:v1:gAAAAA...",
      "wb_refresh_token": "enc:v1:gAAAAA...",
      "uid": "2230906476",
      "login_time": "09-11 01:00",
      "last_test": "",
      "is_valid": true
    }
  ],
  "models": [],
  "tools_passthrough": true,
  "compression_mode": "compress"
}
```

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `api_keys` | 逗号分隔的对外 API Key | `sk-workbuddy` |
| `admin_password` | 管理页密码（落盘加密） | `admin` |
| `workbuddy_accounts` | Desktop 账号列表 | `[]` |
| `models` | 自定义模型列表（空=内置两个 Preview） | `[]` |
| `compression_mode` | `compress` \| `truncation` | `compress` |

**环境变量：**

| 变量 | 说明 | 默认 |
|------|------|------|
| `PORT` | 监听端口 | `8080` |
| `HOST` | 监听地址 | `0.0.0.0` |

## 依赖

- **Python 3.10+**
- FastAPI 0.115
- uvicorn 0.32
- httpx 0.27
- Pydantic v2
- cryptography（配置加密）

```bash
pip install -r requirements.txt
```

## 限制与已知问题

| 限制 | 说明 |
|------|------|
| 模型范围 | 仅 `hy4-preview`、`hy3` |
| cookie DB 锁 | Desktop 运行时可能独占 cookie 库，导入失败先退出 Desktop |
| 会话删除 | 上游无 conversation 删除接口，过期清理只动本地记录 |
| 并发 | 取决于服务端限制，多账号可缓解 |
| 不支持 Embeddings | 仅 Chat / Responses / Anthropic Messages |
| 非流式实际走 SSE | 上游为 SSE，非流式会缓冲后合并返回 |

## 常见问题

**Q: 为什么返回 401 "invalid api key"？**  
A: 检查 `Authorization: Bearer …`。默认 `sk-workbuddy`，在 `config.json` 的 `api_keys` 中修改。

**Q: 为什么返回 503 "no workbuddy account"？**  
A: 管理页尚未导入 Desktop 账号。先登录 WorkBuddy Desktop，退出后自动检测导入。

**Q: 自动检测提示 passToken not found？**  
A:  
1. 确认本机登录过 WorkBuddy Desktop  
2. **退出 Desktop**（它会锁 cookie DB）  
3. 管理页重新点「自动检测」  

**Q: 导入后对话报 session unavailable？**  
A: `passToken` 可能已失效。在 Desktop 重新登录一次，退出后再导入。服务会自动 SSO 续期，一般无需手动刷新。

**Q: 备份要注意什么？**  
A: `config.json` 与 `.secret_key` 必须一起备份。只有 config 没有 key，密文无法解密。

## 许可

MIT

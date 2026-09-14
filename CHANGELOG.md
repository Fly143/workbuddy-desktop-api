# 更新日志（Changelog）

本文件记录 workbuddy-desktop-api 的重要变更。协议层历史继承自 [xiaomi-mimo-desktop-api](https://github.com/Fly143/xiaomi-mimo-desktop-api)。

## [v1.2.3] — 2026-09-12

### 变更
- **主对话 HTTP 超时默认不限** — `MIMO_CLIENT_TIMEOUT` 默认 `0`（`timeout=None`）；思考+输出整条流纯透传，不再被 600s 掐断。需要保护时显式设秒数
- **README 补全 Anthropic 模型别名表** — 与 `_resolve_anthropic_model` 一致（4.7/日期后缀/`-latest`/启发式）

### 修复（RikkaHub / Desktop 选项卡片）
- **Desktop `question` 工具选项可见** — 选项物化到 `content`，避免普通客户端只看到很短正文
- **映射 `question` → RikkaHub `ask_user`** — 仅当客户端只带 `ask_user` 时改写；声明了 `question` 的客户端原样透传，互不影响
- **客户端仅带 `ask_user` 时向上游注入 `question`** — Desktop 只有 tool list 含 `question` 才会触发交互提问

## [v1.2.2] — 2026-09-12

### 变更
- **HTTP 超时默认 600s** — 与 Desktop 对齐，可用 `MIMO_CLIENT_TIMEOUT` 覆盖

## [v1.2.1] — 2026-09-11

### 变更
- **移除手机/APK 手动粘贴导入** — 仅保留 PC 管理页「自动检测」
- **恢复强制 `cryptography` 加密** — 去掉可选导入降级；`config.json` 敏感字段继续 Fernet 密文

## [v1.1.9] — 2026-09-11

### 清理
- 删除未使用的 `_native_tool_calls_to_text`（原生 tool_calls 已直通）

## [v1.1.8] — 2026-09-11

### 清理
- **移除 session_store 死代码（-340 行）** — 上游无状态（无 conversationId），
  指纹匹配 / conv_id 续接 / token 峰值机制自 v1.1.4 修复多轮历史后已无任何消费者：
  - 删除 `app/session_store.py` 与 `sessions.json`
  - `routes.py` / `anthropic_routes.py`：移除 conv_id 传递、指纹记录、token 峰值记录
  - `client.call_api` / `stream_api`：移除被上游忽略的 `conversation_id` 参数
  - `main.py`：移除启动期清理线程与 `threading` / `asyncio` 导入
  - 移除 `/api/cleanup` 端点（调用的 `client.delete_conversations` 方法不存在，该端点一直无法工作）

## [v1.1.7] — 2026-09-11

### 修复
- **Desktop 上游原生 tool_calls 直通** — 实测 WorkBuddy 上游（`copilot.tencent.com/v2/chat/completions`）
  100% OpenAI 兼容，会发送 `delta.tool_calls` 分片累加并以 `finish_reason="tool_calls"` 收尾。
  之前代理层把原生 tool_calls 转成 `TOOL_CALL: name(args)` 文本让 StreamSieve 再解析回 tool_calls，
  绕了一圈。改造：
  - `WorkBuddyClient.stream_api`：原生 `delta.tool_calls` 仅累积不 yield 文本；流结束时 yield
    `{"type": "tool_calls", "calls": merged}` 事件 + `{"type": "finish", "reason": ...}`
  - `WorkBuddyClient.call_api`：原生 `message.tool_calls` 直接作为第五返回值透传，不再混入 content
  - `routes._stream_response`：has_tools 分支处理原生 tool_calls/finish 事件，删 StreamSieve 文本→解析路径
  - `routes.chat_completions`：优先用 call_api 返回的 native_tool_calls；仅无原生响应时才回退文本解析
  - `anthropic_routes.py`：流式 + 非流式同步改造（流式保留 StreamSieve 作为 fallback）
  - `models.OpenAIMessage`：增加 `reasoning` / `reasoning_content` 字段；`_build_response` 在
    非流式响应（含工具调用）中带出 think_content，不再丢失
  - `build_tool_prompt` passthrough=True 改为直接 return ""，prompt 中不塞任何工具指令，
    完全依赖上游原生协议

### 变更（行为）
- prompt 端不再有英文"You have the following tools available..."指令（之前是 fallback 引导，
 现在 Desktop 上游原生协议已足够）

## [v1.1.6] — 2026-09-11

### 修复
- **`test_connection` 探活请求仍塞了 `max_tokens: 1`** — 上次清理默认值时漏掉了这一处。
  `WorkBuddyClient.test_connection`（管理后台"测试连接"按钮与启动期账号健康检查都会调）
  发的最小请求被强制 1 token 上限，与"全部移除限制"指令不一致。
  移除该字段并改注释，由上游/模型自行决定输出长度。
  注：聊天路径（`/v1/chat/completions`、`/v1/messages`、`/v1/responses`）早已透传无默认，
  这次仅清理探活一处。

## [v1.1.5] — 2026-09-11

### 修复
- **带附件的请求必然失败（严重）** — 附件处理从 xiaomi 仓 fork 后一直未适配：
  - 上传请求打到 `https://aistudio.copilot.tencent.com/open-apis/resource/*`，
    该主机在 WorkBuddy 上游并不存在（实测连接失败 / 404）
  - 上传函数读取 `account.service_token`、`account.xiaomichatbot_ph` 等字段，
    而 `WorkBuddyAccount` 只有 `wb_access_token` / `wb_uid` / `wb_refresh_token`，
    访问即 `AttributeError`：**文本文件直接 500，图片被静默丢弃**
  - 结果：任何带图片或文件的请求都无法正常工作

  改为**内联**（不联网、无上传步骤）：
  - 图片 → OpenAI `image_url` + base64 data URL
    实测 `auto` / `hy4-preview` / `glm-5.3` / `kimi-k2.6` 均可正确识图
    （`hy3` 本身不支持视觉，属模型能力差异，非本层问题）
  - 文本文件 → 解码后作为独立 `text` 内容块内联（上限 20 万字符）
  两个函数保留原名与 async 签名，既有调用点无需改动。

- **Anthropic `document` 块被静默丢弃** — `anthropic.convert_messages` 只处理
  `text` / `image` / `tool_result`，附件类文档块直接消失。现转换为 OpenAI `file` 块，
  与图片一样内联进请求（`source.type="text"` 直接作为文本块）

### 清理
- 移除已无调用点的 `build_chunked_queries` — v1.1.4 移除分批 warmup 后成为死代码，
  且其 docstring 恰好宣扬了「靠服务端 conversationId 累积上下文」这一导致 v1.1.4 bug 的错误假设
- 修正 fork 残留的旧上游表述：`routes.py` / `anthropic_routes.py` / `main.py` 的注释与 docstring、
  `config.py` 的工具协议说明、README 目录树

## [v1.1.4] — 2026-09-11

### 修复
- **多轮对话历史完全丢失（严重）** — 上游（`copilot.tencent.com/v2/chat/completions`）
  无状态，没有 conversationId 概念，但代码沿用了 MiMo2API 网页端的会话机制：
  - `continuation=True` 只发最后一条 user 消息 → 上游看不到任何历史
  - 分批 warmup chunk 靠 `conversation_id` 灌历史，
    而 `WorkBuddyClient.call_api` 接收该参数后从未使用（`_query_body` 只构造单条 user 消息），
    warmup 请求发出即丢弃，还白耗一次完整生成

  实测（hy3）：
    第1轮「记住这个数字：5566」
    第2轮 带全量历史问「我刚才让你记住的数字是多少」
    修复前 → 答非所问（看不到历史）
    修复后 → 5566

  改为每次请求都携带完整历史；超长由 `build_query_from_messages` 内的
  QueryGuard 滑动窗口兜底，仍超阈值则按 `compression_mode` 压缩或裁剪。
  影响 chat completions 与 Anthropic Messages 两条路径
  （Responses 路径本就是全量构建，不受影响）。

## [v1.1.3] — 2026-09-11

### 变更
- **Anthropic 转换层移除 `max_tokens` 兜底值** — `convert_request` 不再默认填 4096，
  未显式指定时不写入请求体，透传给上游（与 v1.1.2 对 Chat 路径的处理保持一致）

## [v1.1.0] — 2026-09-11

### 变更（上游切换：MiMo Desktop → WorkBuddy Desktop）
- **凭证来源** — 改为读取 WorkBuddy Desktop 共享凭证文件
  （`%LOCALAPPDATA%/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info`），
  不再解析 Chromium cookie 库、不再复刻小米 passportapi SSO 链路
- **上游** — `https://copilot.tencent.com`
  - 对话：`POST /v2/chat/completions`
  - 模型：`GET /v2/enterprises/personal/models`、`GET /v3/config`
  - 续期：`POST /v2/plugin/auth/token/refresh`
  - 配额：`GET /v2/user/cloudagent/quota`
- **鉴权头** — `Authorization: Bearer <accessToken>` + `X-User-Id` / `X-Domain` /
  `User-Agent: WorkBuddy/<ver>` / `X-IDE-Type` / `X-Product`
  （网关按 UA 解析客户端版本，缺失返回 `check ua, get coding copilot version error`）
- **模型** — 改为云端动态拉取，内置兜底列表
  （`auto` / `hy3` / `hy3-x` / `hy4-preview` / `hy4-preview-f` / `fast-model` /
  `balanced-model` / `deep-model` / `glm-5.3` / `kimi-k2.6` / `deepseek-v4-pro` …）
- **启动自动导入** — 首次启动且无账号时自动导入本机会话，无需手动操作
- **模块改名** — `desktop_session` → `workbuddy_session`，`mimo_client` → `workbuddy_client`；
  账号字段 `mimo_*` → `wb_access_token` / `wb_uid` / `wb_refresh_token`

### 修复
- **非流式请求被上游拒绝** — 上游仅接受 `stream=true`（非流式返回 `11101`），
  改为本地聚合 SSE 后返回完整 `chat.completion`
- **补 `max_tokens` 默认值 4096** — 当时观察到输出被截断而补，实为误判
  （真因是上面的 `clean_tool_text` 顺序问题）。该默认值会吃光思考预算导致正文为空，
  **已于 v1.1.2 移除，改为透传**（详见 v1.1.2 条目）
- **原生 tool_calls 丢失（上游仓库既有 bug）** — `clean_tool_text` 在 `extract_tool_call`
  之前抹掉了 `TOOL_CALL:` 文本，导致 passthrough 模式下工具调用恒为空；调整为先抽取再清洗

## [v1.0.2] — 2026-09-11

### 修复
- **Responses / Claude 带 tools 400** — 扁平 tools 与 `function: null` 规范化为 Chat Completions 格式
- **Claude / Responses 透传 tools** — 流式与非流式均传给 Desktop 上游

### 变更
- **`tools_passthrough` 默认 `true`** — Desktop 走原生 tools/tool_calls

## [v1.0.1] — 2026-09-11

### 修复
- **原生 tool_calls 桥接** — Desktop OpenAI `tool_calls` 转为 `TOOL_CALL` 文本，工具调用链路可用（v1.0.0 会丢弃）
- **reasoning 不再进正文** — `reasoning_content` 包成 `<think>` 块
- **透传 tools** — 请求体带上 OpenAI `tools`；流式按 index 合并分片

## [v1.0.0] — 2026-09-11

### 新增
- **Desktop 会话上游** — 读本机 `workbuddy-desktop.info` 的 `accessToken`，代理 `/v2/chat/completions`
- **独占模型** — `hy4-preview` / `hy3`
- **凭证自动导入** — 管理页一键读本机 Desktop cookie 库
- **Fernet 加密** — `config.json` 敏感字段 + `.secret_key`

### 移除
- 网页 bot（aistudio）上游、Cookie/cURL 导入
- TTS / ASR
- 官方 `api.copilot.tencent.com` 通路

## [v2.2.5] — 2026-05-12

### 新增
- **多语言支持** — 管理面板支持中英双语切换（🌐 EN/中 按钮），涵盖所有 UI 文本
- **英文 README** — 新增 `README_EN.md` 英文版文档，中英互链

## [v2.2.4] — 2026-05-11

### Fixed
- **换行符保留** — `clean_tool_text` 不再 strip 末尾空白

## [v2.2.3] — 2026-05-11

### Fixed
- **工具标签泄漏补全** — `clean_tool_text` 覆盖所有文本输出路径（流式/非流式、OpenAI/Anthropic），新增 `_clean_response_text()` 合并清洗函数。WorkBuddy 原生 `<tool_call>` / `<function=` / `<parameter=` / `<invoke>` 格式全部兜底清理

## [v2.2.2] — 2026-05-11

### Fixed
- **skill 工具参数兼容** — WorkBuddy 模型偶尔把 `skill_view` 的参数名写成 `skill_name`，现自动重映射为 `name`，兼容两种写法。同时覆盖 `skill_manage`、`use_skill`
- **WorkBuddyML 格式泄漏** — 模型输出 WorkBuddyML 格式模板示例（占位符工具名）时，标签不再泄漏到响应正文。`extract_tool_call` 无匹配回退时也清理 WorkBuddyML 残留
- **StreamSieve 清理** — Sieve 捕获 WorkBuddyML 但未解析到工具调用时，使用 `extract_tool_call` 清理后的文本而非原始捕获缓冲

## [v2.3.2] — 2026-05-08

### Added
- **JSON 修复** — `_repair_loose_json()`：未加引号 key、缺失数组括号、非法反斜杠自动修复
- **Schema 归一化** — `_coerce_string_params()`：根据 tool schema 将非字符串值自动转为字符串
- **空参数过滤** — `_has_meaningful_value()`：跳过无实际内容的工具调用参数
- **CDATA 参数保护** — content/command/prompt 等文本参数保留原始字符串
- **CDATA 内嵌围栏块** — `_extract_cdata_safe()`：围栏代码块内的 ]]> 不误判
- **`<br>` 归一化** — `_normalize_br()`：CDATA 中的 `<br>` 标签自动转为换行符

## [v2.3.1] — 2026-05-08

### Added
- **WorkBuddyML 噪声容错** — `strip_workbuddyml()` 支持 7 种格式变体（缺管道、重复 <、全宽、连字符等）
- **围栏代码块保护** — 自动跳过 markdown 代码块内的 WorkBuddyML 示例
- **结构化参数恢复** — `<item>` 子节点转为数组，嵌套 XML 还原对象
- **缺失开标签修复** — 有关闭标签无开头时自动补回
- **HTML 实体解码** — `&lt;` `&gt;` `&amp;` 等自动还原

### Changed
- 策略精简 7→5：删除中文格式和自由文本策略

## [v2.3.0] — 2026-05-08

### Added
- **WorkBuddyML 工具调用格式** — 新增 WorkBuddyML（WorkBuddy Markup Language）格式作为主要工具调用协议
  - `<|WorkBuddyML|tool_calls><|WorkBuddyML|invoke name="X"><|WorkBuddyML|parameter name="K"><![CDATA[V]]></|WorkBuddyML|parameter></|WorkBuddyML|invoke></|WorkBuddyML|tool_calls>`
  - CDATA 包裹解决转义问题，多工具调用天然支持
  - 提升 Roo Code 等 DeepSeek 生态客户端的兼容性
- **策略0：WorkBuddyML 提取** — `strip_workbuddyml()` 将 WorkBuddyML 转为标准 XML 后解析
- **致谢 ds2api** — [CJackHwang/ds2api](https://github.com/CJackHwang/ds2api) DSML 格式设计参考

### Changed
- **工具提示词** — `build_tool_prompt()` 从 `TOOL_CALL:` 格式切换到 WorkBuddyML 格式
- **`clean_tool_text()`** — 新增 WorkBuddyML/CDATA 标签清理正则

## [v2.1.0] — 2026-05-07

### Added
- **Anthropic 模型名映射** — Claude Code CLI 等工具可使用 Anthropic 风格模型名（如 `claude-sonnet-4-6`），内部自动映射为对应 WorkBuddy 模型
  - `claude-opus-4-6` → `workbuddy-v2-pro`
  - `claude-sonnet-4-6` → `workbuddy-v2-flash`
  - `claude-haiku-4-5` → `workbuddy-v2-flash`
  - 支持 search/nothinking 变体及 Claude 3.x/4.x 历史名
- WorkBuddy 原生名（`workbuddy-*`）继续直接可用，`/v1/models` 返回不变

## [v2.0.0] — 2026-05-06

### Added
- **Anthropic Messages API 全兼容** — 新增 9 个 Anthropic 端点：`/v1/messages`（流式/非流式）、count_tokens、message CRUD、batch 全流程
- **多账号管理** — Web 面板增删账号、轮询负载均衡
- **TTS 语音合成**（no-tools）— 声线克隆、音色设计、导演模式

### Changed
- 路由拆分为 `app/anthropic_routes.py`（APIRouter 模式）
- `app/anthropic.py` + `app/batch.py` 模块化

## [Unreleased]

### Changed
- CHANGELOG.md 初始化
- README 补充静默降级 FAQ

---

## [v1.0.0] — 2026-05-04

### Added
- **工具调用** — 6 种提取策略覆盖 TOOL_CALL、JSON、WorkBuddy 原生 XML、`<function_call>`、自由文本匹配、中文 `[调用工具:]` 格式
- **流式筛分（tool_sieve）** — 实时分离流式响应中的正文与工具调用，无需全量缓冲再输出
- **会话管理** — SHA256 消息指纹续接 WorkBuddy conversationId，跨请求保持上下文
- **按模型上下文窗口** — 根据官方 Pricing 页设置精确的 `context_length`/`max_output_tokens`（v2.5-pro/v2-pro/v2.5 为 1M，v2-flash/v2-omni 为 256K）
- **文本文件上传** — 原生 WorkBuddy resource 上传流程（genUploadInfo → PUT OSS → resource/parse），支持 .md/.txt/.py/.json 等
- **用量统计** — 按模型分组的 Token 追踪，Web 面板可视化，支持今日/本周/全部筛选，清空按钮
- **Web 管理面板** — 多 Tab 布局（cURL 导入、Cookie 导入、账号列表、用量统计、API Key 管理）

### Changed
- **双分支架构** — `main`（工具调用）和 `no-tools`（纯对话 + TTS）独立维护
- **工具提示词精简** — 从 30+ 行降到 ~10 行，移到 query 末尾，每次最多注入 6 个工具
- **三轮注入策略** — 首轮完整提示词，后续轮只列工具名（不加行为指令），防止死循环
- **查询格式重排** — 用户消息在前，工具信息在后，跳过 system 消息（WorkBuddy 不支持角色分离）
- **模型列表** — 从 WorkBuddy API 动态发现，未知模型过滤

### Fixed
- **Pydantic v1 兼容** — `model_dump()` 改为 `dict()`（项目依赖 pydantic<2）
- **TOOL_CALL 文本泄露** — 流式筛分实时截获并过滤工具调用文本
- **camelCase 工具名不匹配** — `_resolve_tool_name()` 四级匹配（直接/忽略大小写/驼峰转蛇形/模糊）
- **工具结果标签泄露** — `_strip_tool_result_blocks()` 覆盖 3 种格式：`[TOOL_RESULT]`、`[tool_result id=xxx]`、`<tool_result>`
- **工具调用死循环** — 三轮注入策略防止重复调用同一工具
- **工具提示词被截断丢弃** — 截断后重新插入工具信息
- **空参数工具调用失败** — 正则 `(.+?)` → `(.*?)` 允许 `getTimeInfo()`
- **流式沉默间隙** — `_safe_flush()` 只保留 `<think>`/`</think>` 部分后缀，不吞内容
- **图片模型劫持** — 移除强制切到 omni 的逻辑，用户选择什么模型就走什么模型
- **cURL 添加账号失败** — `update_config()` 增加字段过滤，拒绝 `token_masked`
- **RikkaHub 流式延迟** — reasoning 实时流式，有工具时仅正文缓冲
- **Cookie 字符串解析** — 支持粘贴整段 `key=value; key=value` Cookie header
- **保存按钮无反馈** — 所有保存按钮增加 disabled + loading 文本

### 已知问题
- serviceToken 约 24 小时过期，需网页端退出重新登录（仅刷新 Cookie 无效）
- **静默降级：** Token 过期后，基础聊天（flash/pro）和"测试连接"仍显示正常，但 `workbuddy-v2.5` / `workbuddy-v2-omni` 多模态识图会静默失效。如果只聊天空正常但识图不工作，优先怀疑凭证过期
- WorkBuddy 服务端并发限制：约 1-2 请求/账号
- 不支持 Embeddings 端点
- 非原生 function calling（通过文本提示模拟）

---

## [0.x] — 初期开发阶段

从 [Water008/MiMo2API](https://github.com/Water008/MiMo2API) fork 后的早期改版（网页直接上传文件，无 git 历史记录），包含以下功能沉淀：

- OpenAI 兼容 `/v1/chat/completions`、`/v1/models` 端点
- 多账号轮询负载均衡
- Cookie / cURL 凭证导入 + Web 管理面板
- 图片上传（genUploadInfo → PUT → resource/parse → multiMedias）
- Think 块分离（`<think>`/`</think>`）
- Termux/Android 部署脚本
- 功能文档 README

---

## 分支说明

| 分支 | 功能 |
|------|------|
| `main` | 工具调用（6 策略）、流式筛分、会话管理、文件上传 |
| `no-tools` | 纯对话代理 + TTS（语音合成、音色设计、语音克隆、导演模式） |

日常使用推荐 no-tools 分支（上下文更干净，输出质量更高）。如需 TTS 功能直接使用 no-tools。

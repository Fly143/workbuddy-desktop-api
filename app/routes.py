"""API路由 — WorkBuddy Desktop-api

OpenAI 兼容接口 / 模型发现 / 管理后台 / Desktop 账号管理。
"""

import time
import uuid
import json
import asyncio
import re
import httpx
from datetime import datetime as _dt
from typing import Optional, Tuple
from pathlib import Path
from fastapi import APIRouter, HTTPException, Header, Request, Depends
from fastapi.responses import StreamingResponse, Response, JSONResponse
from .auth import verify_admin
from .models import (
    OpenAIRequest, OpenAIResponse, OpenAIChoice, OpenAIMessage,
    OpenAIDelta, OpenAIUsage, ParseCurlRequest, TestAccountRequest
)
from .config import config_manager, WorkBuddyAccount
from .workbuddy_client import WorkBuddyClient, WorkBuddyApiError
from .utils import parse_curl, build_query_from_messages, extract_medias_from_messages, upload_media_to_workbuddy, upload_text_file_to_workbuddy
from .context_compressor import compress_messages, truncate_messages, should_compress
from .tool_call import extract_tool_call, normalize_tool_call, get_tool_names, clean_tool_text  # build_tool_prompt unused
from .tool_sieve import StreamSieve
from .usage_store import add_usage as _add_usage, get_usage as _get_usage, clear_usage as _clear_usage
from .response_store import (
    save_response_record as _save_response_record,
    get_response_record as _get_response_record,
    delete_response_record as _delete_response_record,
    update_response_record as _update_response_record,
)

router = APIRouter()

# ─── 常量 ─────────────────────────────────────────────────────

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"

MODELS_CONFIG_URL = ""  # WorkBuddy Desktop 通路无需单独的模型配置端点

# ─── 模型上下文参数 ───────────────────────────────────────────

def _model_context(model_id: str) -> dict:
    m = model_id.lower()
    if "preview" in m or m.startswith("workbuddy-x-"):
        return {"context_length": 262144, "max_output_tokens": 32768}
    return None

_models_cache = None
_models_lock = asyncio.Lock()


# ─── API Key 验证 ─────────────────────────────────────────────

def validate_api_key(authorization: Optional[str]) -> bool:
    if not authorization:
        return False
    key = authorization.replace("Bearer ", "").strip()
    return config_manager.validate_api_key(key)


# 云端 /v2/enterprises/personal/models 不可达时的兜底列表
DESKTOP_MODELS = [
    "auto",
    "hy3",
    "hy3-x",
    "hy4-preview",
    "hy4-preview-f",
    "fast-model",
    "balanced-model",
    "deep-model",
    "glm-5.3",
    "glm-5.3-flash",
    "kimi-k2.6",
    "kimi-k2.5",
    "deepseek-v4-pro",
    "deepseek-v4-flash",
    "minimax-m2.7",
]


def _append_extra_models(models: list) -> list:
    for m in DESKTOP_MODELS:
        if m not in models:
            models.append(m)
    return models


async def _do_discover() -> list:
    """优先拉云端账号可用模型，失败回退内置列表。"""
    global _models_cache
    models = list(DESKTOP_MODELS)
    try:
        from .workbuddy_session import list_remote_models

        accounts = config_manager.config.workbuddy_accounts
        creds = {}
        if accounts:
            acc = accounts[0]
            creds = {
                "wb_access_token": acc.wb_access_token or None,
                "wb_refresh_token": acc.wb_refresh_token or None,
                "wb_uid": acc.wb_uid or None,
            }
        remote = await list_remote_models(creds)
        if remote:
            models = remote
    except Exception as e:
        print(f"[模型发现] 云端列表拉取失败，使用内置列表: {e}")
    async with _models_lock:
        _models_cache = models
    return models


async def discover_models() -> list:
    if config_manager.config.models:
        return _append_extra_models(config_manager.config.models.copy())
    return await _do_discover()


def get_models_list() -> list:
    if config_manager.config.models:
        return _append_extra_models(config_manager.config.models.copy())
    if _models_cache is not None:
        return _models_cache
    return list(DESKTOP_MODELS)


async def _background_refresh():
    try:
        await _do_discover()
    except Exception as e:
        print(f"[模型发现] 后台刷新失败: {e}")


@router.get("/v1/models")
async def list_models(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
):
    api_key = authorization or (f"Bearer {x_api_key}" if x_api_key else None)
    if not validate_api_key(api_key):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key"}})
    asyncio.create_task(_background_refresh())
    models = get_models_list()
    ctx_items = [(m, _model_context(m)) for m in models]
    data = []
    for m, ctx in ctx_items:
        obj = {"id": m, "object": "model", "created": 1681940951, "owned_by": "xiaomi"}
        if ctx:
            obj.update({
                "context_length": ctx["context_length"],
                "context_window": ctx["context_length"],
                "max_input_tokens": ctx["context_length"],
                "max_output_tokens": ctx["max_output_tokens"],
            })
        data.append(obj)
    return {"object": "list", "data": data}


@router.post("/v1/models/refresh")
async def refresh_models(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
):
    api_key = authorization or (f"Bearer {x_api_key}" if x_api_key else None)
    if not validate_api_key(api_key):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key"}})
    models = await discover_models()
    ctx_items = [(m, _model_context(m)) for m in models]
    data = []
    for m, ctx in ctx_items:
        obj = {"id": m, "object": "model", "created": 1681940951, "owned_by": "xiaomi"}
        if ctx:
            obj.update({
                "context_length": ctx["context_length"],
                "context_window": ctx["context_length"],
                "max_input_tokens": ctx["context_length"],
                "max_output_tokens": ctx["max_output_tokens"],
            })
        data.append(obj)
    return {"object": "list", "data": data}


@router.get("/v1/models/{model_id}")
async def get_model(
    model_id: str,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
):
    api_key = authorization or (f"Bearer {x_api_key}" if x_api_key else None)
    if not validate_api_key(api_key):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key"}})
    models = get_models_list()
    if model_id in models:
        ctx = _model_context(model_id)
        base = {
            "id": model_id, "object": "model", "created": 1681940951, "owned_by": "xiaomi",
        }
        if ctx:
            base.update({
                "context_length": ctx["context_length"],
                "context_window": ctx["context_length"],
                "max_input_tokens": ctx["context_length"],
                "max_output_tokens": ctx["max_output_tokens"],
            })
        return base
    raise HTTPException(status_code=404, detail={"error": {"message": f"Model {model_id} not found"}})


# ─── 文本清洗辅助函数 ────────────────────────────────────────

def _strip_tool_result_blocks(text: str) -> str:
    """移除模型幻觉输出的 TOOL_RESULT 标签。

    模型看到上下文中 [TOOL_RESULT] 和 <tool_result> 格式后学会复述。
    移除所有已知格式。
    """
    if not text:
        return text
    cleaned = re.sub(r'\[TOOL_RESULT\]\s*', '', text, flags=re.IGNORECASE)
    cleaned = re.sub(r'\[/TOOL_RESULT\]\s*', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\[tool_result\s+id=\S+\]\s*', '', cleaned, flags=re.IGNORECASE)
    # XML 格式: <tool_result>...</tool_result>（模型学会的另一种格式）
    cleaned = re.sub(r'</?tool_result>\s*', '', cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _camel_case(name: str) -> str:
    """snake_case -> camelCase: web_search -> webSearch"""
    parts = name.split('_')
    return parts[0] + ''.join(p.capitalize() for p in parts[1:])


def _strip_tool_name_prefix(text: str, tool_names: list) -> str:
    """去掉模型作为独立 SSE 事件输出的工具名（如 'webSearch'）。

    处理 snake_case 和 camelCase 变体，大小写不敏感。
    """
    if not text or not tool_names:
        return text
    variants = []
    for n in tool_names:
        variants.append(re.escape(n))
        if '_' in n:
            variants.append(re.escape(_camel_case(n)))
    escaped = '|'.join(variants)
    cleaned = re.sub(rf'^({escaped})\s*\n?', '', text.strip(), flags=re.IGNORECASE)
    return cleaned.strip()


def _strip_workbuddy_prefix(text: str) -> str:
    """通用 WorkBuddy 原生前缀清理（含 IGNORECASE）。

    在 workbuddy_client 层已过滤 SSE 事件，此处做兜底。
    """
    if not text:
        return text
    prefixes = ['webSearch', 'getTimeInfo', 'getTime', 'sessionSearch',
                'imageSearch', 'fileSearch', 'getLocation', 'webExtract',
                'getWeather', 'calculator']
    escaped = '|'.join(re.escape(p) for p in prefixes)
    cleaned = re.sub(rf'^({escaped})\s*\n?', '', text.strip(), flags=re.IGNORECASE)
    return cleaned.strip()


def _clean_response_text(text: str, tool_names: list = None) -> str:
    """综合文本清理管道：TOOL_RESULT + 工具前缀 + WorkBuddy前缀 + 工具文本残留。"""
    text = _strip_tool_result_blocks(text)
    if tool_names:
        text = _strip_tool_name_prefix(text, tool_names)
    text = _strip_workbuddy_prefix(text)
    text = clean_tool_text(text)
    return text


# ─── Think 标签处理 ──────────────────────────────────────────

def _safe_flush(text: str) -> Tuple[str, str]:
    """分割文本为 (安全发送, 保留在缓冲区)。

    仅保留可能是 <think> 或 </think> 部分标签的最长后缀。
    其余全部立即刷新，避免 silence gap 导致客户端进入缓冲模式。
    """
    last_lt = text.rfind('<')
    if last_lt == -1:
        return text, ""
    suffix = text[last_lt:]
    if THINK_OPEN.startswith(suffix) or THINK_CLOSE.startswith(suffix):
        return text[:last_lt], suffix
    return text, ""


def _split_think(text: str) -> Tuple[str, str]:
    """从文本中分离 think 块和正文。

    Returns: (main_content, think_content)
    """
    start = text.find(THINK_OPEN)
    if start == -1:
        return text, ""

    end = text.find(THINK_CLOSE, start)
    if end == -1:
        return text[:start].strip(), text[start + len(THINK_OPEN):]

    think_content = text[start + len(THINK_OPEN):end]
    main = text[:start] + text[end + len(THINK_CLOSE):]
    return main.strip(), think_content


# ─── 响应构建 ─────────────────────────────────────────────────

def _question_options_text(tool_calls: list) -> str:
    """把 WorkBuddy `AskUserQuestion` / RikkaHub `ask_user` 转成可见选项文本。"""
    ask_names = {"askuserquestion", "ask_user_question", "ask_followup_question", "question", "ask_user"}
    blocks = []
    for tc in tool_calls or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        name = (fn.get("name") or "").lower()
        if name not in ask_names:
            continue
        raw = fn.get("arguments") or ""
        try:
            args = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except (json.JSONDecodeError, TypeError):
            continue
        questions = args.get("questions") or []
        if isinstance(questions, dict):
            questions = [questions]
        for i, q in enumerate(questions, 1):
            if not isinstance(q, dict):
                continue
            title = (q.get("question") or "").strip() or f"选项 {i}"
            opts = q.get("options") or []
            lines = [f"### {title}"]
            multi = q.get("multiSelect") is True or q.get("multiple") is True or (q.get("selection_type") or "") == "multi"
            if multi:
                lines.append("（可多选）")
            for j, opt in enumerate(opts, 1):
                if isinstance(opt, str):
                    label, desc = opt, ""
                elif isinstance(opt, dict):
                    label = (opt.get("label") or "").strip()
                    desc = (opt.get("description") or "").strip()
                else:
                    continue
                if not label:
                    continue
                line = f"{j}. {label}"
                if desc:
                    line += f" — {desc}"
                lines.append(line)
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks).strip()


def _merge_question_visibility(content: str | None, tool_calls: list | None) -> str | None:
    """保证交互提问选项在 content 中可见（RikkaHub 有 tool_calls 时也可能只看正文）。"""
    qtext = _question_options_text(tool_calls)
    if not qtext:
        return content
    base = (content or "").strip()
    if not base:
        return qtext
    if qtext in base:
        return content
    return base + "\n\n" + qtext


def _question_only_tool_calls(tool_calls: list | None) -> bool:
    """是否仅为交互提问类工具（无其它业务工具）。"""
    ask = {"askuserquestion", "ask_user_question", "ask_followup_question", "question", "ask_user"}
    names = set()
    for tc in tool_calls or []:
        fn = (tc or {}).get("function") or {}
        name = (fn.get("name") or "").strip().lower()
        if name:
            names.add(name)
    return bool(names) and names.issubset(ask)


def _option_label(opt) -> str:
    if isinstance(opt, str):
        return opt.strip()
    if isinstance(opt, dict):
        label = (opt.get("label") or "").strip()
        desc = (opt.get("description") or "").strip()
        if label and desc:
            return f"{label} — {desc}"
        return label or desc
    return ""


def _rewrite_question_to_ask_user(tool_calls: list) -> list:
    """把 WorkBuddy `AskUserQuestion` 改写成 RikkaHub 本地工具 `ask_user`。

    WorkBuddy schema（app.asar）:
      questions: [{ question, header?, options:[{label,description}] (2-4), multiSelect? }] (1-4)
    RikkaHub schema:
      questions: [{ id, question, options: string[], selection_type: text|single|multi }]
    注意：WorkBuddy **不是** MiMo Desktop 的 `question` 工具。
    """
    ask_names = {"askuserquestion", "ask_user_question", "ask_followup_question", "question", "ask_user"}
    out = []
    qi = 0
    for tc in tool_calls or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        name = (fn.get("name") or "").lower()
        if name not in ask_names:
            out.append(tc)
            continue
        try:
            args = json.loads(fn.get("arguments") or "{}") if isinstance(fn.get("arguments"), str) else (fn.get("arguments") or {})
        except (json.JSONDecodeError, TypeError):
            out.append(tc)
            continue
        if name == "ask_user":
            qs = args.get("questions") or []
            changed = False
            new_qs = []
            for q in qs:
                if isinstance(q, dict) and not q.get("id"):
                    qi += 1
                    q = {**q, "id": f"q{qi}"}
                    changed = True
                new_qs.append(q)
            if not changed:
                out.append(tc)
                continue
            args = {**args, "questions": new_qs}
        else:
            qs_in = args.get("questions") or []
            if isinstance(qs_in, dict):
                qs_in = [qs_in]
            new_qs = []
            for q in qs_in:
                if not isinstance(q, dict):
                    continue
                qi += 1
                opts = q.get("options") or []
                labels = [x for x in (_option_label(o) for o in opts) if x]
                if q.get("multiSelect") is True or q.get("multiple") is True:
                    st = "multi"
                elif labels:
                    st = "single"
                else:
                    st = "text"
                new_qs.append({
                    "id": q.get("id") or f"q{qi}",
                    "question": q.get("question") or "",
                    "options": labels,
                    "selection_type": st,
                })
            args = {"questions": new_qs}
        out.append({
            **tc,
            "type": "function",
            "function": {
                "name": "ask_user",
                "arguments": json.dumps(args, ensure_ascii=False),
            },
        })
    return out


def _desktop_question_tool() -> dict:
    """WorkBuddy `AskUserQuestion` 工具定义（非 MiMo question）。

    schema 来自 app.asar：questions 1-4；options 2-4，label≤50，header≤12，可选 multiSelect。
    """
    return {
        "type": "function",
        "function": {
            "name": "AskUserQuestion",
            "description": (
                "Ask the user one or more clarification questions with selectable options. "
                "Use when you need the user to choose before continuing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 4,
                        "items": {
                            "type": "object",
                            "properties": {
                                "question": {"type": "string"},
                                "header": {"type": "string", "maxLength": 12},
                                "options": {
                                    "type": "array",
                                    "minItems": 2,
                                    "maxItems": 4,
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "label": {"type": "string", "maxLength": 50},
                                            "description": {"type": "string"},
                                        },
                                        "required": ["label"],
                                    },
                                },
                                "multiSelect": {"type": "boolean"},
                            },
                            "required": ["question", "options"],
                        },
                    }
                },
                "required": ["questions"],
            },
        },
    }


def _client_has_ask_user_only(tools_dict: list | None) -> bool:
    """客户端只声明了 RikkaHub ask_user、未声明 WorkBuddy 提问工具。"""
    if not tools_dict:
        return False
    names = set()
    for t in tools_dict:
        fn = (t or {}).get("function") or {}
        n = (fn.get("name") or t.get("name") or "").lower()
        if n:
            names.add(n)
    wb_ask = {"askuserquestion", "ask_user_question", "ask_followup_question", "question"}
    has_rikka = "ask_user" in names
    has_wb = bool(names & wb_ask)
    return has_rikka and not has_wb


def _ensure_desktop_question_tool(tools_dict: list | None) -> list | None:
    """客户端带 RikkaHub `ask_user` 时，向 WorkBuddy 补注入 `AskUserQuestion`。"""
    if not tools_dict:
        return tools_dict
    names = set()
    for t in tools_dict:
        fn = (t or {}).get("function") or {}
        n = (fn.get("name") or t.get("name") or "").lower()
        if n:
            names.add(n)
    wb_ask = {"askuserquestion", "ask_user_question", "ask_followup_question", "question"}
    if names & wb_ask or "ask_user" not in names:
        return tools_dict
    return list(tools_dict) + [_desktop_question_tool()]


def _build_response(
    msg_id: str, model: str,
    content: str = None, tool_calls: list = None,
    reasoning: str = None,
    finish_reason: str = "stop", usage: dict = None
) -> OpenAIResponse:
    """统一构建 OpenAI 非流式响应。"""
    message = OpenAIMessage(
        role="assistant", content=content, tool_calls=tool_calls,
        reasoning=reasoning, reasoning_content=reasoning,
    )
    usage_obj = None
    if usage:
        usage_obj = OpenAIUsage(
            prompt_tokens=usage.get("promptTokens", 0),
            completion_tokens=usage.get("completionTokens", 0),
            total_tokens=usage.get("promptTokens", 0) + usage.get("completionTokens", 0)
        )
    return OpenAIResponse(
        id=msg_id, object="chat.completion",
        created=int(time.time()), model=model,
        choices=[OpenAIChoice(index=0, message=message, finish_reason=finish_reason)],
        usage=usage_obj or OpenAIUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)
    )


def _normalize_native_tool_calls(native_tool_calls: list) -> list:
    """把上游 message.tool_calls 规整为 OpenAI 标准格式。

    上游结构：{"id": ..., "type": "function", "function": {"name": ..., "arguments": "..."}}
    这里只补 type 缺失、做基本字段校验，不改 id 与 function。
    """
    result = []
    for tc in native_tool_calls or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        if not fn.get("name"):
            continue
        item = dict(tc)
        item.setdefault("type", "function")
        result.append(item)
    return result


def _build_chunk(
    msg_id: str, model: str,
    content: str = None, reasoning: str = None,
    tool_calls: list = None, finish_reason: str = None,
    role: str = None, created: int = None
) -> str:
    """统一构建 SSE chunk 字符串。

    exclude_none=True 去除 null 字段，避免客户端因 message:null
    等非标准字段误判为非流式模式。
    reasoning 同时输出 reasoning 和 reasoning_content（RikkaHub 兼容）。
    """
    delta = OpenAIDelta(
        role=role, content=content,
        reasoning=reasoning, tool_calls=tool_calls
    )
    chunk = OpenAIResponse(
        id=msg_id, object="chat.completion.chunk",
        created=created if created is not None else int(time.time()),
        model=model,
        choices=[OpenAIChoice(index=0, delta=delta, finish_reason=finish_reason)]
    )
    data = chunk.dict(exclude_none=True)
    if reasoning:
        for choice in data.get('choices', []):
            d = choice.get('delta', {})
            if 'reasoning' in d:
                d['reasoning_content'] = reasoning
    return f"data: {json.dumps(data)}\n\n"


# ─── 聊天接口 ─────────────────────────────────────────────────

@router.post("/v1/chat/completions")
async def chat_completions(
    request: OpenAIRequest,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
):
    api_key = authorization or (f"Bearer {x_api_key}" if x_api_key else None)
    if not validate_api_key(api_key):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key"}})

    """OpenAI兼容的聊天接口。"""

    # # 请求日志（发版时关闭）
    # try:
    #     print(f"[REQ] model={request.model} stream={request.stream} "
    #           f"tools={len(request.tools) if request.tools else 0} "
    #           f"tool_choice={request.tool_choice} reasoning_effort={request.reasoning_effort}")
    #     try:
    #         logf = Path.home() / 'workbuddy_requests.log'
    #         if logf.exists() and logf.stat().st_size > 5 * 1024 * 1024:
    #             logf.write_text('')
    #         with open(str(logf), 'a') as rf:
    #             import datetime as dt2
    #             full = request.model_dump(exclude_none=True)
    #             full['_timestamp'] = dt2.datetime.now().isoformat()
    #             rf.write(json.dumps(full, ensure_ascii=False) + '\n')
    #     except Exception:
    #         pass
    # except Exception:
    #     pass

    account = config_manager.get_next_account()
    if not account:
        raise HTTPException(status_code=503, detail={"error": {"message": "no workbuddy account"}})

    # 转换 tools 为字典列表
    tools_dict = [t.dict() if hasattr(t, 'dict') else t for t in request.tools] if request.tools else None
    prefer_ask_user = _client_has_ask_user_only(tools_dict)
    tools_dict = _ensure_desktop_question_tool(tools_dict)

    # 提取媒体和文本文件
    query_text, base64_medias, text_files, processed_msgs = extract_medias_from_messages(request.messages)
    effective_model = request.model


    multi_medias = []
    if base64_medias:
        for media in base64_medias:
            media_obj = await upload_media_to_workbuddy(
                media["base64"], media["mimeType"], account, effective_model
            )
            if media_obj:
                multi_medias.append(media_obj)

    # 文本文件：解码后内联为文本块（上游无文件上传接口）
    if text_files:
        for tf in text_files:
            media_obj = await upload_text_file_to_workbuddy(
                tf["base64"], tf["filename"], tf["mimeType"], account, effective_model
            )
            if media_obj:
                multi_medias.append(media_obj)

    # 构建查询
    passthrough_mode = request.passthrough or config_manager.config.tools_passthrough

    # 思考强度纯透传：客户端传了才开启并下传
    reasoning_effort = (request.reasoning_effort or "").strip().lower() or None
    thinking = bool(reasoning_effort)
    client = WorkBuddyClient(account)

    # 上游无状态（copilot.tencent.com/v2/chat/completions 没有 conversation 概念）：
    # 每次请求都必须携带完整历史。增量发送（continuation）与分批 warmup 都依赖
    # 服务端 conversationId 累积上下文，在此上游上会被静默丢弃：历史丢失，
    # 且每个 warmup chunk 白耗一次完整生成。
    # 超长由 build_query_from_messages 内的 QueryGuard 滑动窗口兜底，
    # 仍超阈值则按 compression_mode 压缩或裁剪。
    needs_compression = should_compress(request.messages)

    if not needs_compression:
        query = build_query_from_messages(
            request.messages, tools=tools_dict, passthrough=passthrough_mode
        )
    else:
        # 超长：根据 compression_mode 选择处理方式
        mode = config_manager.config.compression_mode
        if mode == "compress":
            # LLM 压缩模式：流式响应内部先提示再压缩
            query = None
        else:
            # 裁剪模式：直接裁剪，不需要 LLM 调用
            request.messages = truncate_messages(request.messages)
            query = build_query_from_messages(
                request.messages, tools=tools_dict, passthrough=passthrough_mode
            )
            needs_compression = False  # 已裁剪，不需要流式压缩

    # 流式响应
    if request.stream:
        return StreamingResponse(
            _stream_response(client, query, thinking, effective_model, tools_dict, multi_medias, passthrough=passthrough_mode,
                             needs_compression=needs_compression,
                             raw_messages=request.messages if needs_compression else None,
                             raw_tools=tools_dict if needs_compression else None,
                             raw_passthrough=passthrough_mode if needs_compression else None,
                             effective_model=effective_model,
                             reasoning_effort=reasoning_effort,
                             prefer_ask_user=prefer_ask_user),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            }
        )

    # 非流式响应
    if needs_compression:
        # 静默压缩（用户等待中，不额外提示）
        _, request.messages = await compress_messages(request.messages, effective_model, client)
        query = build_query_from_messages(
            request.messages, tools=tools_dict, passthrough=passthrough_mode
        )
    try:
        content, think_content, usage, citations, native_tool_calls = await client.call_api(
            query, thinking, effective_model, multi_medias,
            tools=tools_dict, reasoning_effort=reasoning_effort)

        # 保存用量
        if usage:
            _add_usage(request.model, usage.get("promptTokens", 0), usage.get("completionTokens", 0))
        # 优先用上游原生 tool_calls（passthrough=True 的主流路径）；
        # 仅在没有原生响应时才回退到文本解析（passthrough=False / 上游异常）。
        tool_names = []
        tool_calls = None
        if native_tool_calls:
            tool_calls = _normalize_native_tool_calls(native_tool_calls)
        if tools_dict and not tool_calls:
            tool_names = get_tool_names(tools_dict)
            result = extract_tool_call(content, tool_names)
            if result:
                if result[0]:
                    tool_calls = result[0]
                if result[1] is not None:
                    content = result[1]

        # 清理模型输出杂质
        content = _strip_tool_result_blocks(content)

        msg_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

        # 清洗工具名前缀
        # 未命中工具调用时兜底清洗标记残留；命中时 extract_tool_call 已返回清洗后文本，不可再 clean（会抹掉标记）
        if not tool_calls:
            content = clean_tool_text(content)
        content = _strip_tool_name_prefix(content, tool_names)

        if tool_calls:
            visible = _merge_question_visibility(content, tool_calls)
            out_calls = _rewrite_question_to_ask_user(tool_calls) if prefer_ask_user else tool_calls
            return _build_response(
                msg_id, request.model,
                content=visible, tool_calls=out_calls,
                reasoning=think_content,
                finish_reason="tool_calls", usage=usage
            )
        else:
            full_content = content
            if think_content:
                full_content = f"{THINK_OPEN}{think_content}{THINK_CLOSE}\n{content}"
            return _build_response(
                msg_id, request.model,
                content=full_content,
                reasoning=think_content,
                finish_reason="stop", usage=usage
            )

    except WorkBuddyApiError as e:
        raise HTTPException(status_code=e.status_code, detail={"error": {"message": f"WorkBuddy API: {e.response_body[:200]}"}})
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail={"error": {"message": str(e)}})


async def _stream_response(
    client: WorkBuddyClient, query: str, thinking: bool, model: str,
    tools: list = None, multi_medias: list = None,
    passthrough: bool = False,
    needs_compression: bool = False,
    raw_messages: list = None,
    raw_tools: list = None,
    raw_passthrough: bool = False,
    effective_model: str = None,
    prefer_ask_user: bool = False,
    reasoning_effort: str | None = None,
):
    """流式响应生成器。

    行为矩阵：
    | 场景              | reasoning  | content             |
    | 无 tools          | 流式       | 流式                |
    | 有 tools+无工具调用 | 流式      | 流式                |
    | 有 tools+有工具调用 | 流式      | 不发（发 tool_calls）|

    修复 v4.1：
    - 统一使用 full_content 进行工具调用提取（而非 content_buffer）
    - 捕获 httpx.ReadTimeout
    - 无工具调用时也清理工具名前缀
    """
    msg_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created_t = int(time.time())

    # 初始 role delta
    yield _build_chunk(msg_id, model, created=created_t, role="assistant")

    # 压缩模式：先提示，再根据模式裁剪/压缩，然后重建 query
    if needs_compression and raw_messages:
        mode = "compress"  # 默认 LLM 压缩（裁剪模式已在 chat_completions 里处理）
        if mode == "compress":
            yield _build_chunk(msg_id, model, created=created_t, content="正在压缩上下文，请稍候...")
            print(f"[ContextCompressor] 开始 LLM 压缩 {len(raw_messages)} 条消息")
            _, compressed_msgs = await compress_messages(raw_messages, effective_model, client)
        else:
            yield _build_chunk(msg_id, model, created=created_t, content="正在裁剪上下文，请稍候...")
            compressed_msgs = truncate_messages(raw_messages)
        query = build_query_from_messages(
            compressed_msgs, tools=raw_tools, passthrough=raw_passthrough
        )
        yield _build_chunk(msg_id, model, created=created_t, content="处理完成，正在生成回复...")

    has_tools = tools is not None

    try:
        if has_tools:
            # ═══════════════════════════════════════════════════
            # 有工具定义：上游原生 OpenAI tool_calls 直接透传
            # 正文经 think 标签解析后流式输出（无工具调用时）
            # ═══════════════════════════════════════════════════
            tool_names = get_tool_names(tools)
            collected_tool_calls = []  # 原生 tool_calls 累积
            content_buffer_chunks = []
            in_think = False
            buffer = ""
            last_usage = None
            upstream_finish_reason = ""

            async for sse_data in client.stream_api(
                query, thinking, model, multi_medias, tools=tools,
                reasoning_effort=reasoning_effort,
            ):
                ev_type = sse_data.get("type")
                if ev_type == "tool_calls":
                    # 上游原生 tool_calls（已按 index 合并）→ 直接累加
                    for tc in sse_data.get("calls", []) or []:
                        if tc.get("function", {}).get("name"):
                            collected_tool_calls.append(tc)
                    continue
                if ev_type == "finish":
                    upstream_finish_reason = sse_data.get("reason") or "stop"
                    continue
                if ev_type == "usage":
                    last_usage = sse_data
                    continue
                chunk = sse_data.get("content", "")
                if not chunk:
                    continue

                buffer += chunk.replace("\x00", "")

                # 处理 think 标签
                while True:
                    if not in_think:
                        idx = buffer.find(THINK_OPEN)
                        if idx != -1:
                            safe, keep = _safe_flush(buffer[:idx])
                            if safe:
                                clean = _clean_response_text(safe, tool_names)
                                if clean:
                                    content_buffer_chunks.append(clean)
                            in_think = True
                            buffer = buffer[idx + len(THINK_OPEN):]
                            continue

                        safe, keep = _safe_flush(buffer)
                        if safe:
                            clean = _clean_response_text(safe, tool_names)
                            if clean:
                                content_buffer_chunks.append(clean)
                        buffer = keep
                        break
                    else:
                        idx = buffer.find(THINK_CLOSE)
                        if idx != -1:
                            safe, keep = _safe_flush(buffer[:idx])
                            if safe:
                                yield _build_chunk(msg_id, model, created=created_t, reasoning=safe)
                            in_think = False
                            buffer = buffer[idx + len(THINK_CLOSE):]
                            continue

                        safe, keep = _safe_flush(buffer)
                        if safe:
                            yield _build_chunk(msg_id, model, created=created_t, reasoning=safe)
                        buffer = keep
                        break

            # 正文留在 buffer 中的追加
            if buffer and not in_think:
                clean = _clean_response_text(buffer, tool_names)
                if clean:
                    content_buffer_chunks.append(clean)
            elif buffer and in_think:
                yield _build_chunk(msg_id, model, created=created_t, reasoning=buffer)

            if collected_tool_calls:
                visible = _merge_question_visibility("".join(content_buffer_chunks), collected_tool_calls)
                out_calls = (
                    _rewrite_question_to_ask_user(collected_tool_calls)
                    if prefer_ask_user else collected_tool_calls
                )
                if visible:
                    yield _build_chunk(msg_id, model, created=created_t, content=visible)
                streaming_tc = []
                for i, tc in enumerate(out_calls):
                    item = {**tc, "index": i}
                    streaming_tc.append(item)
                yield _build_chunk(msg_id, model, created=created_t,
                                   tool_calls=streaming_tc,
                                   finish_reason=upstream_finish_reason or "tool_calls")
                yield "data: [DONE]\n\n"
                if last_usage:
                    _add_usage(model, last_usage.get("promptTokens", 0), last_usage.get("completionTokens", 0))
                return

            # 无工具调用：补发缓冲正文后再发 stop
            for _buffered in content_buffer_chunks:
                yield _build_chunk(msg_id, model, created=created_t, content=_buffered)
            yield _build_chunk(msg_id, model, created=created_t, finish_reason="stop")
            yield "data: [DONE]\n\n"
            if last_usage:
                _add_usage(model, last_usage.get("promptTokens", 0), last_usage.get("completionTokens", 0))

        else:
            # ═══════════════════════════════════════════════════
            # 无工具定义：实时流式输出
            # ═══════════════════════════════════════════════════
            buffer = ""
            in_think = False
            last_usage = None

            pending_text = ""
            async for sse_data in client.stream_api(
                query, thinking, model, multi_medias, tools=tools,
                reasoning_effort=reasoning_effort,
            ):
                if sse_data.get("type") == "usage":
                    last_usage = sse_data
                    continue
                chunk = sse_data.get("content", "")
                if not chunk:
                    continue

                buffer += chunk.replace("\x00", "")

                while True:
                    if not in_think:
                        idx = buffer.find(THINK_OPEN)
                        if idx != -1:
                            safe, keep = _safe_flush(buffer[:idx])
                            if safe:
                                clean = _clean_response_text(safe)
                                if clean:
                                    yield _build_chunk(msg_id, model, created=created_t, content=clean)
                            in_think = True
                            buffer = buffer[idx + len(THINK_OPEN):]
                            continue

                        safe, keep = _safe_flush(buffer)
                        if safe:
                            clean = _clean_response_text(safe)
                            if clean:
                                yield _build_chunk(msg_id, model, created=created_t, content=clean)
                        buffer = keep
                        break
                    else:
                        idx = buffer.find(THINK_CLOSE)
                        if idx != -1:
                            safe, keep = _safe_flush(buffer[:idx])
                            if safe:
                                yield _build_chunk(msg_id, model, created=created_t, reasoning=safe)
                            in_think = False
                            buffer = buffer[idx + len(THINK_CLOSE):]
                            continue

                        safe, keep = _safe_flush(buffer)
                        if safe:
                            yield _build_chunk(msg_id, model, created=created_t, reasoning=safe)
                        buffer = keep
                        break

            # 发送剩余内容
            if buffer:
                clean = _clean_response_text(buffer)
                if clean:
                    if in_think:
                        yield _build_chunk(msg_id, model, created=created_t, reasoning=clean)
                    else:
                        yield _build_chunk(msg_id, model, created=created_t, content=clean)

            yield _build_chunk(msg_id, model, created=created_t, finish_reason="stop")
            yield "data: [DONE]\n\n"
            if last_usage:
                _add_usage(model, last_usage.get("promptTokens", 0), last_usage.get("completionTokens", 0))

    except httpx.ReadTimeout:
        # 连接读取超时 — 发送优雅结束
        yield _build_chunk(msg_id, model, created=created_t, finish_reason="length")
        yield "data: [DONE]\n\n"
    except WorkBuddyApiError as e:
        error_data = {"error": {"message": f"WorkBuddy API {e.status_code}: {e.response_body[:200]}",
                                "type": "upstream_error", "code": e.status_code}}
        yield f"data: {json.dumps(error_data)}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"=== STREAM ERROR ===\n{tb}")
        yield f"data: {json.dumps({'error': {'message': str(e)}})}\n\n"
        yield "data: [DONE]\n\n"



# ─── 管理页面 ─────────────────────────────────────────────────

from pathlib import Path as _Path
_ADMIN_HTML = (_Path(__file__).parent.parent / "web" / "index.html").read_text(encoding="utf-8")


@router.get("/admin")
@router.get("/")
async def admin_page(username: str = Depends(verify_admin)):
    from starlette.responses import HTMLResponse
    return HTMLResponse(_ADMIN_HTML)


# ─── 账号管理 API ─────────────────────────────────────────────

import re as _re
from datetime import datetime as _dt


@router.get("/api/accounts")
async def list_accounts(username: str = Depends(verify_admin)):
    accounts = []
    for acc in config_manager.config.workbuddy_accounts:
        token = acc.wb_access_token
        masked = token[:8] + "..." + token[-4:] if len(token) > 16 else ("***" if token else "")
        accounts.append({
            "uid": acc.uid,
            "user_id": acc.user_id,
            "token_masked": masked,
            "has_session": acc.has_session(),
            "is_valid": acc.is_valid,
            "login_time": acc.login_time,
            "last_test": acc.last_test,
        })
    return {"accounts": accounts}


@router.get("/api/desktop/auto-import")
async def desktop_auto_import(username: str = Depends(verify_admin)):
    from .auto_import import auto_import_desktop
    return await auto_import_desktop()


@router.post("/api/desktop/import")
async def desktop_import(request: Request, username: str = Depends(verify_admin)):
    from .auto_import import apply_import_payload

    data = await request.json()
    fields = apply_import_payload(data)
    if not fields["wb_access_token"]:
        return {"ok": False, "error": "Need wbAccessToken from Desktop cookie store"}

    return await _validate_and_save(
        fields["wb_access_token"],
        fields["wb_uid"],
        fields["wb_refresh_token"],
        fields.get("uid") or "",
    )


async def _validate_and_save(
    wb_access_token: str,
    wb_uid: str = "",
    wb_refresh_token: str = "",
    uid: str = "",
):
    """导入 Desktop passToken：落库 + 会话探活。"""
    now = _dt.now().strftime("%m-%d %H:%M")
    uid = uid or wb_uid or ""

    existing = False
    for i, acc in enumerate(config_manager.config.workbuddy_accounts):
        if uid and (acc.uid == uid or acc.wb_uid == uid):
            config_manager.config.workbuddy_accounts[i] = WorkBuddyAccount(
                wb_access_token=wb_access_token or acc.wb_access_token,
                wb_uid=wb_uid or acc.wb_uid,
                wb_refresh_token=wb_refresh_token or acc.wb_refresh_token,
                uid=uid or acc.uid,
                login_time=now,
                is_valid=True,
            )
            existing = True
            break
    if not existing:
        config_manager.config.workbuddy_accounts.append(WorkBuddyAccount(
            wb_access_token=wb_access_token,
            wb_uid=wb_uid,
            wb_refresh_token=wb_refresh_token,
            uid=uid,
            login_time=now,
            is_valid=True,
        ))
    config_manager.save()

    acc = config_manager.config.workbuddy_accounts[-1] if not existing else next(
        a for a in config_manager.config.workbuddy_accounts if a.uid == uid or a.wb_uid == uid
    )
    ok, msg = await WorkBuddyClient(acc).test_connection()
    return {"ok": True, "validated": ok, "detail": msg, "user_id": uid}


@router.delete("/api/accounts/{idx}")
async def delete_account(idx: int, username: str = Depends(verify_admin)):
    accounts = config_manager.config.workbuddy_accounts
    if idx < 0 or idx >= len(accounts):
        raise HTTPException(404, "account not found")
    removed = accounts.pop(idx)
    config_manager.save()
    return {"ok": True, "removed_user_id": removed.user_id}


@router.post("/api/accounts/{idx}/test")
async def test_account(idx: int, username: str = Depends(verify_admin)):
    accounts = config_manager.config.workbuddy_accounts
    if idx < 0 or idx >= len(accounts):
        raise HTTPException(404, "account not found")

    from .workbuddy_client import WorkBuddyClient
    acc = accounts[idx]
    ok, msg = await WorkBuddyClient(acc).test_connection()
    acc.is_valid = ok
    acc.last_test = _dt.now().strftime("%m-%d %H:%M")
    config_manager.save()
    return {"ok": ok, "detail": msg}


# ─── 旧版管理接口（保留兼容） ────────────────────────────────

@router.get("/api/config")
async def get_config(username: str = Depends(verify_admin)):
    return config_manager.get_config()


@router.post("/api/config")
async def update_config(request: Request, username: str = Depends(verify_admin)):
    try:
        new_config = await request.json()
        config_manager.update_config(new_config)
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": "invalid"})


@router.post("/api/parse-curl")
async def parse_curl_command(request: ParseCurlRequest, username: str = Depends(verify_admin)):
    account = parse_curl(request.curl)
    if not account:
        raise HTTPException(status_code=400, detail={"error": "parse failed"})
    return account.to_dict()


@router.post("/api/test-account")
async def test_account_endpoint(request: TestAccountRequest, username: str = Depends(verify_admin)):
    try:
        account = WorkBuddyAccount(
            wb_access_token=request.wb_access_token,
            wb_uid=request.wb_uid,
            wb_refresh_token=request.wb_refresh_token,
            uid=request.wb_uid,
        )
        client = WorkBuddyClient(account)
        content, _, _, _, _ = await client.call_api("hi", False)
        return {"success": True, "response": content}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ─── 用量统计 API ─────────────────────────────────────────────

@router.get("/api/usage")
async def usage_stats(username: str = Depends(verify_admin)):
    """返回用量统计：按模型分组 + 全部汇总。"""
    return _get_usage()


@router.delete("/api/usage")
async def clear_usage(username: str = Depends(verify_admin)):
    """清空全部用量统计数据。"""
    _clear_usage()
    return {"ok": True}




# ─── 模型列表（免鉴权，供管理页面使用） ───────────────────────

@router.get("/api/models")
async def admin_models():
    """返回可用模型列表（无鉴权，仅供管理页面动态加载）。"""
    return {"models": get_models_list()}
# ═══════════════════════════════════════════════════════════════
# OpenAI Responses API 兼容层
# 追加到 admin_models() 函数之后
# ═══════════════════════════════════════════════════════════════

# ─── 常量 ────────────────────────────────────────────────────

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"

_RESPONSE_TERMINAL_STATUSES = {"completed", "failed", "incomplete", "cancelled"}

# ─── 辅助函数 ─────────────────────────────────────────────────

def _gen_response_id() -> str:
    """生成响应ID：resp_ + uuid4 hex[:32]"""
    return f"resp_{uuid.uuid4().hex[:32]}"


def _response_text_config(body: dict) -> dict:
    """从 body 中提取 text.format 配置。"""
    text = body.get("text")
    if isinstance(text, dict):
        cfg = dict(text)
        fmt = cfg.get("format")
        if isinstance(fmt, dict):
            cfg["format"] = dict(fmt)
        elif isinstance(fmt, str):
            cfg["format"] = {"type": fmt}
        else:
            cfg["format"] = {"type": "text"}
        return cfg
    return {"format": {"type": "text"}}


def _json_schema_from_text_config(text_config: dict | None) -> dict | None:
    """从 text_config 中提取 JSON Schema 字典。"""
    fmt = text_config.get("format") if isinstance(text_config, dict) else None
    if not isinstance(fmt, dict) or fmt.get("type") != "json_schema":
        return None
    schema = fmt.get("schema")
    if isinstance(schema, dict):
        return schema
    json_schema = fmt.get("json_schema")
    if isinstance(json_schema, dict):
        nested = json_schema.get("schema")
        return nested if isinstance(nested, dict) else json_schema
    return None


def _response_text_item(text: str, item_id: str | None = None) -> dict:
    """构建 OpenAI Responses output_text item。"""
    return {
        "id": item_id or f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{
            "type": "output_text",
            "text": text or "",
            "annotations": [],
        }],
    }


def _response_reasoning_item(summary_text: str, item_id: str | None = None) -> dict:
    """构建 reasoning summary item。"""
    return {
        "id": item_id or f"rs_{uuid.uuid4().hex[:24]}",
        "type": "reasoning",
        "summary": [{
            "type": "summary_text",
            "text": summary_text or "",
        }],
    }


def _response_function_call_item(tool_call: dict, call_id: str | None = None) -> dict:
    """从 tool_call 构建 function_call item。"""
    fn = tool_call.get("function", {}) if isinstance(tool_call, dict) else {}
    return {
        "id": f"fc_{uuid.uuid4().hex[:24]}",
        "type": "function_call",
        "call_id": call_id or tool_call.get("id") or f"call_{uuid.uuid4().hex[:24]}",
        "name": fn.get("name", ""),
        "arguments": fn.get("arguments", "{}"),
        "status": "completed",
    }


def _make_response_object(
    response_id: str, model: str, status: str,
    items: list, usage: dict | None,
    incomplete_details: dict | None = None,
) -> dict:
    """构建完整的 Response 对象字典。"""
    text_cfg = {"format": {"type": "text"}}
    output_text = ""
    for item in items or []:
        if item.get("type") == "message":
            for part in item.get("content", []) or []:
                if part.get("type") == "output_text":
                    output_text = part.get("text", "") or ""
    usage_obj = None
    if usage:
        input_tok = usage.get("prompt_tokens") or usage.get("promptTokens") or 0
        output_tok = usage.get("completion_tokens") or usage.get("completionTokens") or 0
        total_tok = usage.get("total_tokens") or (input_tok + output_tok)
        usage_obj = {
            "input_tokens": int(input_tok),
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": int(output_tok),
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": int(total_tok),
        }
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "error": None,
        "incomplete_details": incomplete_details,
        "instructions": None,
        "max_output_tokens": None,
        "model": model,
        "output": items or [],
        "parallel_tool_calls": True,
        "previous_response_id": None,
        "reasoning": {},
        "store": True,
        "temperature": None,
        "text": text_cfg,
        "tool_choice": "auto",
        "tools": [],
        "top_p": None,
        "truncation": "disabled",
        "usage": usage_obj,
        "user": None,
        "metadata": {},
        "output_text": output_text,
    }


def _convert_response_input_to_messages(input_data) -> list[dict]:
    """将 Responses API input 格式转换为 OpenAI Chat 消息列表。"""
    messages: list[dict] = []
    if input_data is None:
        return messages
    items = input_data if isinstance(input_data, list) else [input_data]
    for item in items:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        role = item.get("role")

        if role in ("system", "user", "assistant", "tool"):
            content = item.get("content", "")
            if isinstance(content, list):
                parts = []
                assistant_tool_calls = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type")
                    if ptype in ("input_text", "output_text", "text"):
                        parts.append({"type": "text", "text": part.get("text", "")})
                    elif ptype == "input_image":
                        image_url = part.get("image_url") or part.get("url") or ""
                        if image_url:
                            parts.append({"type": "image_url", "image_url": {"url": image_url}})
                    elif ptype == "input_file":
                        parts.append({
                            "type": "file",
                            "file": {
                                "filename": part.get("filename") or "file.txt",
                                "file_data": part.get("file_data") or part.get("data") or "",
                            }
                        })
                    elif ptype == "function_call" and role == "assistant":
                        assistant_tool_calls.append({
                            "id": part.get("call_id") or part.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                            "type": "function",
                            "function": {
                                "name": part.get("name", ""),
                                "arguments": part.get("arguments", "{}") if isinstance(part.get("arguments", "{}"), str)
                                else json.dumps(part.get("arguments", {}), ensure_ascii=False),
                            }
                        })
                msg = {"role": role, "content": parts if parts else ""}
                if assistant_tool_calls:
                    msg["tool_calls"] = assistant_tool_calls
                    if not parts:
                        msg["content"] = None
                messages.append(msg)
            else:
                msg = {"role": role, "content": content}
                if role == "assistant" and item_type == "function_call":
                    msg["tool_calls"] = [{
                        "id": item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                        "type": "function",
                        "function": {
                            "name": item.get("name", ""),
                            "arguments": item.get("arguments", "{}") if isinstance(item.get("arguments", "{}"), str)
                            else json.dumps(item.get("arguments", {}), ensure_ascii=False),
                        }
                    }]
                    if content in ("", None):
                        msg["content"] = None
                messages.append(msg)
            continue

        if item_type == "message":
            content = item.get("content", [])
            role = item.get("role", "user")
            parts = []
            assistant_tool_calls = []
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type")
                    if ptype in ("input_text", "output_text", "text"):
                        parts.append({"type": "text", "text": part.get("text", "")})
                    elif ptype == "input_image":
                        image_url = part.get("image_url") or part.get("url") or ""
                        if image_url:
                            parts.append({"type": "image_url", "image_url": {"url": image_url}})
                    elif ptype == "input_file":
                        parts.append({
                            "type": "file",
                            "file": {
                                "filename": part.get("filename") or "file.txt",
                                "file_data": part.get("file_data") or part.get("data") or "",
                            }
                        })
                    elif ptype == "function_call" and role == "assistant":
                        assistant_tool_calls.append({
                            "id": part.get("call_id") or part.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                            "type": "function",
                            "function": {
                                "name": part.get("name", ""),
                                "arguments": part.get("arguments", "{}") if isinstance(part.get("arguments", "{}"), str)
                                else json.dumps(part.get("arguments", {}), ensure_ascii=False),
                            }
                        })
            msg = {"role": role, "content": parts if parts else ""}
            if assistant_tool_calls:
                msg["tool_calls"] = assistant_tool_calls
                if not parts:
                    msg["content"] = None
            messages.append(msg)
            continue

        if item_type == "function_call_output":
            output = item.get("output", "")
            if isinstance(output, dict):
                output = json.dumps(output, ensure_ascii=False)
            elif output is None:
                output = ""
            tool_message = {"role": "tool", "content": str(output)}
            if item.get("call_id"):
                tool_message["tool_call_id"] = item.get("call_id")
            messages.append(tool_message)
            continue

        if item_type in ("input_text", "text"):
            messages.append({"role": "user", "content": item.get("text", "")})

    return messages


def _normalize_structured_output_text(output_text: str, text_config: dict | None) -> str:
    """对 structured output (json_object/json_schema) 规范化输出文本。"""
    if not output_text or not isinstance(text_config, dict):
        return output_text
    fmt = text_config.get("format")
    if not isinstance(fmt, dict):
        return output_text
    fmt_type = fmt.get("type")
    if fmt_type not in ("json_object", "json_schema"):
        return output_text

    # 尝试从文本中提取 JSON
    candidate = output_text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"\s*```$", "", candidate)
    start_candidates = [i for i in (candidate.find("{"), candidate.find("[")) if i != -1]
    if start_candidates:
        start = min(start_candidates)
        end_candidates = [candidate.rfind("}"), candidate.rfind("]")]
        end = max(end_candidates)
        if end > start:
            candidate = candidate[start:end + 1]
    try:
        parsed = json.loads(candidate)
        return json.dumps(parsed, ensure_ascii=False)
    except (json.JSONDecodeError, ValueError, TypeError):
        return output_text


# ─── Token 计数辅助 ──────────────────────────────────────────

def _count_response_input_tokens(
    input_value,
    instructions: str | None = None,
    tools: list[dict] | None = None,
) -> int:
    """估算 input tokens：字符数 / 4 的简单启发式方法。"""
    total_chars = 0
    if isinstance(input_value, list):
        serialized = json.dumps(input_value, ensure_ascii=False)
        total_chars += len(serialized)
    elif isinstance(input_value, str):
        total_chars += len(input_value)
    if instructions:
        total_chars += len(instructions)
    if tools:
        total_chars += len(json.dumps(tools, ensure_ascii=False))
    return max(1, total_chars // 4)


def _count_tokens(text: str) -> int:
    """简单 token 估算：字符数 / 4。"""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _build_response_usage(usage: dict | None) -> dict:
    """将 WorkBuddy usage 转换为 Responses API usage 格式。"""
    usage = usage or {}
    input_tokens = int(usage.get("promptTokens", 0) or usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("completionTokens", 0) or usage.get("completion_tokens", 0) or usage.get("output_tokens", 0) or 0)
    total_tokens = int(usage.get("total_tokens", 0) or usage.get("totalTokens", 0) or (input_tokens + output_tokens))
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": total_tokens,
    }


def _extract_output_text(output: list[dict]) -> str:
    """从 output items 列表中提取文本内容。"""
    texts: list[str] = []
    for item in output or []:
        if item.get("type") == "message":
            for content in item.get("content", []) or []:
                if content.get("type") == "output_text":
                    texts.append(content.get("text", "") or "")
    return "".join(texts)


def _response_output_from_message(msg: dict) -> list[dict]:
    """从 OpenAI chat message 构建 Responses output items。"""
    output: list[dict] = []
    reasoning = msg.get("reasoning_content", "")
    if reasoning:
        output.append(_response_reasoning_item(reasoning))
    content = msg.get("content", "")
    if isinstance(content, str) and content:
        output.append(_response_text_item(content))
    tool_calls = msg.get("tool_calls") or []
    for tc in tool_calls:
        output.append(_response_function_call_item(tc))
    if not output:
        output.append(_response_text_item(""))
    return output


def _response_status_from_finish_reason(finish_reason: str) -> str:
    if finish_reason in ("stop", "tool_calls"):
        return "completed"
    if finish_reason in ("length", "content_filter"):
        return "incomplete"
    return "completed"


def _sse_json(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


# ─── 核心函数：非流式 ────────────────────────────────────────

async def _do_response_chat(body: dict, account) -> tuple:
    """非流式 Responses API 聊天。

    Returns:
        (model_used, usage_dict, items_list)
    """
    model = body.get("model", "default")
    input_data = body.get("input", [])
    instructions = body.get("instructions")
    tools = body.get("tools")
    text_config = _response_text_config(body)

    # 转换 input 为消息列表
    messages = _convert_response_input_to_messages(input_data)

    # 处理 instructions：作为 system 消息前置
    if isinstance(instructions, str) and instructions.strip():
        messages.insert(0, {"role": "system", "content": instructions.strip()})

    # structured output 处理
    structured_format = text_config.get("format", {}).get("type")
    if structured_format in ("json_object", "json_schema"):
        has_system = any(m.get("role") == "system" for m in messages)
        if structured_format == "json_schema":
            schema = _json_schema_from_text_config(text_config)
            instruction_text = "Please respond with valid JSON matching this schema."
        else:
            schema = None
            instruction_text = "Please respond with valid JSON object."
        sys_instruction = {"role": "system", "content": instruction_text}
        if has_system:
            # 追加到最后一条 system 消息之后
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "system":
                    messages.insert(i + 1, sys_instruction)
                    break
        else:
            messages.insert(0, sys_instruction)

    # 转换 dict 消息为 OpenAIMessage 对象
    openai_messages = []
    for m in messages:
        openai_messages.append(OpenAIMessage(
            role=m.get("role", "user"),
            content=m.get("content"),
            tool_calls=m.get("tool_calls"),
            tool_call_id=m.get("tool_call_id"),
        ))

    # 提取媒体
    query_text, base64_medias, text_files, processed_msgs = extract_medias_from_messages(openai_messages)
    effective_model = model

    multi_medias = []
    if base64_medias:
        for media in base64_medias:
            media_obj = await upload_media_to_workbuddy(
                media["base64"], media["mimeType"], account, effective_model
            )
            if media_obj:
                multi_medias.append(media_obj)
    if text_files:
        for tf in text_files:
            media_obj = await upload_text_file_to_workbuddy(
                tf["base64"], tf["filename"], tf["mimeType"], account, effective_model
            )
            if media_obj:
                multi_medias.append(media_obj)

    # 构建 tools dict
    tools_dict = [dict(t) if hasattr(t, 'dict') else t for t in tools] if tools else None

    # 构建查询（超长则根据模式裁剪/压缩）
    client = WorkBuddyClient(account)
    if should_compress(openai_messages):
        mode = config_manager.config.compression_mode
        if mode == "compress":
            _, openai_messages = await compress_messages(openai_messages, effective_model, client)
        else:
            openai_messages = truncate_messages(openai_messages)
    query = build_query_from_messages(
        openai_messages, tools=tools_dict, passthrough=config_manager.config.tools_passthrough
    )

    thinking = False
    try:
        content, think_content, usage, citations, native_tool_calls = await client.call_api(
            query, thinking, effective_model, multi_medias, tools=tools_dict
        )
    except WorkBuddyApiError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail={"error": {"message": f"WorkBuddy API: {e.response_body[:200]}"}}
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail={"error": {"message": str(e)}})

    # 清理输出
    content = _strip_tool_result_blocks(content)

    # 额外处理：模型可能输出多个 think 块，_parse_think_tags 只剥除了第一个
    remaining_thinks = []
    cleaned_content = re.sub(
        r'<think>(.*?)</think>',
        lambda m: remaining_thinks.append(m.group(1).strip()) or '',
        content,
        flags=re.DOTALL
    )
    # 过滤空 think 块（模型可能输出 <think></think>）
    remaining_thinks = [t for t in remaining_thinks if t]
    if remaining_thinks:
        content = cleaned_content.strip()
        extra_think = '\n'.join(remaining_thinks)
        think_content = (think_content + '\n' + extra_think).strip() if think_content else extra_think

    # 构建 items
    items = []
    has_thinking = False
    if think_content:
        items.append(_response_reasoning_item(think_content))
        has_thinking = True

    # 工具调用提取
    tool_names = []
    tool_calls = None
    if tools_dict:
        tool_names = get_tool_names(tools_dict)
        result = extract_tool_call(content, tool_names)
        if result:
            if result[0]:
                tool_calls = result[0]
            if result[1] is not None:
                content = result[1]  # 使用清理后的文本（含 WorkBuddyML 残留清理）

    # 未命中工具调用时兜底清洗标记残留；命中时 extract_tool_call 已返回清洗后文本，不可再 clean（会抹掉标记）
    if not tool_calls:
        content = clean_tool_text(content)
    content = _strip_tool_name_prefix(content, tool_names)

    if tool_calls:
        for tc in tool_calls:
            items.append(_response_function_call_item(tc))
    elif content:
        normalized = _normalize_structured_output_text(content, text_config)
        items.append(_response_text_item(normalized))

    if not items:
        items.append(_response_text_item(""))

    return effective_model, usage, items


# ─── 核心函数：流式 ──────────────────────────────────────────

async def _stream_response_events(body: dict, account):
    """流式 Responses API 事件生成器。

    Yields:
        event dicts (response.created, response.output_text.delta, etc.)
    """
    model = body.get("model", "default")
    input_data = body.get("input", [])
    instructions = body.get("instructions")
    tools = body.get("tools")
    text_config = _response_text_config(body)

    # 转换 input 为消息列表（同非流式）
    messages = _convert_response_input_to_messages(input_data)
    if isinstance(instructions, str) and instructions.strip():
        messages.insert(0, {"role": "system", "content": instructions.strip()})

    structured_format = text_config.get("format", {}).get("type")
    if structured_format in ("json_object", "json_schema"):
        has_system = any(m.get("role") == "system" for m in messages)
        if structured_format == "json_schema":
            instruction_text = "Please respond with valid JSON matching this schema."
        else:
            instruction_text = "Please respond with valid JSON object."
        sys_instruction = {"role": "system", "content": instruction_text}
        if has_system:
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "system":
                    messages.insert(i + 1, sys_instruction)
                    break
        else:
            messages.insert(0, sys_instruction)

    openai_messages = []
    for m in messages:
        openai_messages.append(OpenAIMessage(
            role=m.get("role", "user"),
            content=m.get("content"),
            tool_calls=m.get("tool_calls"),
            tool_call_id=m.get("tool_call_id"),
        ))

    query_text, base64_medias, text_files, processed_msgs = extract_medias_from_messages(openai_messages)
    effective_model = model

    multi_medias = []
    if base64_medias:
        for media in base64_medias:
            media_obj = await upload_media_to_workbuddy(
                media["base64"], media["mimeType"], account, effective_model
            )
            if media_obj:
                multi_medias.append(media_obj)
    if text_files:
        for tf in text_files:
            media_obj = await upload_text_file_to_workbuddy(
                tf["base64"], tf["filename"], tf["mimeType"], account, effective_model
            )
            if media_obj:
                multi_medias.append(media_obj)

    tools_dict = [dict(t) if hasattr(t, 'dict') else t for t in tools] if tools else None

    # 构建查询（超长则根据模式裁剪/压缩）— 与 _do_response_chat 保持一致
    client = WorkBuddyClient(account)
    if should_compress(openai_messages):
        mode = config_manager.config.compression_mode
        if mode == "compress":
            _, openai_messages = await compress_messages(openai_messages, effective_model, client)
        else:
            openai_messages = truncate_messages(openai_messages)
    query = build_query_from_messages(
        openai_messages, tools=tools_dict, passthrough=config_manager.config.tools_passthrough
    )
    thinking = False

    response_id = body.get("_response_id") or _gen_response_id()
    created_t = int(time.time())

    # 初始事件
    init_payload = {
        "id": response_id,
        "object": "response",
        "created_at": created_t,
        "status": "in_progress",
        "model": effective_model,
        "output": [],
        "usage": None,
    }
    yield {"type": "response.created", "response": dict(init_payload)}
    yield {"type": "response.in_progress", "response": dict(init_payload)}

    reasoning_parts: list[str] = []
    api_usage: dict = {}  # 收集API返回的准确usage数据
    text_parts: list[str] = []
    reasoning_item_id = f"rs_{uuid.uuid4().hex[:24]}"
    message_item_id = f"msg_{uuid.uuid4().hex[:24]}"
    output_started: set[str] = set()
    output_indices: dict[str, int] = {}
    content_started = False
    tool_calls_map: dict[int, dict] = {}

    def _start_output_item(item: dict) -> tuple[int, dict | None]:
        item_id = item.get("id") or f"out_{len(output_indices)}"
        if item_id not in output_indices:
            output_indices[item_id] = len(output_indices)
        output_index = output_indices[item_id]
        if item_id not in output_started:
            output_started.add(item_id)
            return_event = {
                "type": "response.output_item.added",
                "output_index": output_index,
                "item": item,
            }
            return output_index, return_event
        return output_index, None

    client = WorkBuddyClient(account)
    has_tools = tools_dict is not None

    try:
        if has_tools:
            # 有工具定义：使用 StreamSieve
            tool_names = get_tool_names(tools_dict)
            sieve = StreamSieve(
                mode='tool_call',
                parse_fn=lambda text: extract_tool_call(text, tool_names),
            )
            in_think = False
            buffer = ""

            async for sse_data in client.stream_api(
                query, thinking, effective_model, multi_medias, tools=tools_dict
            ):
                if sse_data.get("type") == "usage":
                    api_usage = sse_data
                    continue
                chunk = sse_data.get("content", "")
                if not chunk:
                    continue

                buffer += chunk.replace("\x00", "")

                while True:
                    if not in_think:
                        idx = buffer.find(THINK_OPEN)
                        if idx != -1:
                            safe, keep = _safe_flush(buffer[:idx])
                            if safe:
                                for ev in sieve.feed(safe):
                                    if ev.type == 'text':
                                        clean = _clean_response_text(ev.data, tool_names)
                                        if clean:
                                            text_parts.append(clean)
                                            if not content_started:
                                                content_started = True
                                                item = _response_text_item("", message_item_id)
                                                oi, start_evt = _start_output_item(item)
                                                if start_evt:
                                                    yield start_evt
                                                yield {
                                                    "type": "response.content_part.added",
                                                    "item_id": message_item_id,
                                                    "output_index": oi,
                                                    "content_index": 0,
                                                    "part": item["content"][0],
                                                }
                                            yield {
                                                "type": "response.output_text.delta",
                                                "item_id": message_item_id,
                                                "output_index": output_indices.get(message_item_id, 0),
                                                "content_index": 0,
                                                "delta": clean,
                                            }
                            in_think = True
                            buffer = buffer[idx + len(THINK_OPEN):]
                            continue

                        safe, keep = _safe_flush(buffer)
                        if safe:
                            for ev in sieve.feed(safe):
                                if ev.type == 'text':
                                    clean = _clean_response_text(ev.data, tool_names)
                                    if clean:
                                        text_parts.append(clean)
                                        if not content_started:
                                            content_started = True
                                            item = _response_text_item("", message_item_id)
                                            oi, start_evt = _start_output_item(item)
                                            if start_evt:
                                                yield start_evt
                                            yield {
                                                "type": "response.content_part.added",
                                                "item_id": message_item_id,
                                                "output_index": oi,
                                                "content_index": 0,
                                                "part": item["content"][0],
                                            }
                                        yield {
                                            "type": "response.output_text.delta",
                                            "item_id": message_item_id,
                                            "output_index": output_indices.get(message_item_id, 0),
                                            "content_index": 0,
                                            "delta": clean,
                                        }
                                elif ev.type == 'tool_calls':
                                    for tc in ev.data:
                                        idx = len(tool_calls_map)
                                        fc_item = _response_function_call_item(tc)
                                        fc_id = fc_item["id"]
                                        tool_calls_map[idx] = {
                                            "id": fc_id,
                                            "call_id": fc_item.get("call_id", fc_id),
                                            "name": tc.get("function", {}).get("name", ""),
                                            "arguments": tc.get("function", {}).get("arguments", "{}"),
                                            "status": "completed",
                                        }
                                        # output_item.added 不预填 arguments
                                        added_item = {k: v for k, v in fc_item.items() if k != "arguments"}
                                        oi, start_evt = _start_output_item(added_item)
                                        if start_evt:
                                            yield start_evt
                                        # 始终通过 delta 传递参数
                                        args_str = fc_item.get("arguments", "{}")
                                        yield {
                                            "type": "response.function_call_arguments.delta",
                                            "item_id": fc_id,
                                            "output_index": oi,
                                            "delta": args_str,
                                        }
                        buffer = keep
                        break
                    else:
                        idx = buffer.find(THINK_CLOSE)
                        if idx != -1:
                            safe, keep = _safe_flush(buffer[:idx])
                            if safe:
                                reasoning_parts.append(safe)
                                if len(reasoning_parts) == len(safe) == len(safe):
                                    pass
                                item = _response_reasoning_item("", reasoning_item_id)
                                oi, start_evt = _start_output_item(item)
                                if start_evt:
                                    yield start_evt
                                yield {
                                    "type": "response.reasoning_text.delta",
                                    "item_id": reasoning_item_id,
                                    "output_index": output_indices.get(reasoning_item_id, 0),
                                    "content_index": 0,
                                    "delta": safe,
                                }
                            in_think = False
                            buffer = buffer[idx + len(THINK_CLOSE):]
                            continue

                        safe, keep = _safe_flush(buffer)
                        if safe:
                            reasoning_parts.append(safe)
                            if len(reasoning_parts) == len(safe):
                                item = _response_reasoning_item("", reasoning_item_id)
                                oi, start_evt = _start_output_item(item)
                                if start_evt:
                                    yield start_evt
                            yield {
                                "type": "response.reasoning_text.delta",
                                "item_id": reasoning_item_id,
                                "output_index": output_indices.get(reasoning_item_id, 0),
                                "content_index": 0,
                                "delta": safe,
                            }
                        buffer = keep
                        break

            # Flush buffer
            if buffer and not in_think:
                for ev in sieve.feed(buffer):
                    if ev.type == 'text':
                        clean = _clean_response_text(ev.data, tool_names)
                        if clean:
                            text_parts.append(clean)
                            if not content_started:
                                content_started = True
                                item = _response_text_item("", message_item_id)
                                oi, start_evt = _start_output_item(item)
                                if start_evt:
                                    yield start_evt
                                yield {
                                    "type": "response.content_part.added",
                                    "item_id": message_item_id,
                                    "output_index": oi,
                                    "content_index": 0,
                                    "part": item["content"][0],
                                }
                            yield {
                                "type": "response.output_text.delta",
                                "item_id": message_item_id,
                                "output_index": output_indices.get(message_item_id, 0),
                                "content_index": 0,
                                "delta": clean,
                            }

            for ev in sieve.flush():
                if ev.type == 'text':
                    clean = _clean_response_text(ev.data, tool_names)
                    if clean:
                        text_parts.append(clean)
                        if not content_started:
                            content_started = True
                            item = _response_text_item("", message_item_id)
                            oi, start_evt = _start_output_item(item)
                            if start_evt:
                                yield start_evt
                            yield {
                                "type": "response.content_part.added",
                                "item_id": message_item_id,
                                "output_index": oi,
                                "content_index": 0,
                                "part": item["content"][0],
                            }
                        yield {
                            "type": "response.output_text.delta",
                            "item_id": message_item_id,
                            "output_index": output_indices.get(message_item_id, 0),
                            "content_index": 0,
                            "delta": clean,
                        }
                elif ev.type == 'tool_calls':
                    for tc in ev.data:
                        idx = len(tool_calls_map)
                        fc_item = _response_function_call_item(tc)
                        fc_id = fc_item["id"]
                        tool_calls_map[idx] = {
                            "id": fc_id,
                            "call_id": fc_item.get("call_id", fc_id),
                            "name": tc.get("function", {}).get("name", ""),
                            "arguments": tc.get("function", {}).get("arguments", "{}"),
                            "status": "completed",
                        }
                        # output_item.added 不预填 arguments
                        added_item = {k: v for k, v in fc_item.items() if k != "arguments"}
                        oi, start_evt = _start_output_item(added_item)
                        if start_evt:
                            yield start_evt
                        # 始终通过 delta 传递参数
                        args_str = fc_item.get("arguments", "{}")
                        yield {
                            "type": "response.function_call_arguments.delta",
                            "item_id": fc_id,
                            "output_index": oi,
                            "delta": args_str,
                        }

        else:
            # 无工具：简单流式
            in_think = False
            buffer = ""

            async for sse_data in client.stream_api(query, thinking, effective_model, multi_medias):
                if sse_data.get("type") == "usage":
                    api_usage = sse_data
                    continue
                chunk = sse_data.get("content", "")
                if not chunk:
                    continue

                buffer += chunk.replace("\x00", "")

                while True:
                    if not in_think:
                        idx = buffer.find(THINK_OPEN)
                        if idx != -1:
                            safe, keep = _safe_flush(buffer[:idx])
                            if safe:
                                clean = _clean_response_text(safe)
                                if clean:
                                    text_parts.append(clean)
                                    if not content_started:
                                        content_started = True
                                        item = _response_text_item("", message_item_id)
                                        oi, start_evt = _start_output_item(item)
                                        if start_evt:
                                            yield start_evt
                                        yield {
                                            "type": "response.content_part.added",
                                            "item_id": message_item_id,
                                            "output_index": oi,
                                            "content_index": 0,
                                            "part": item["content"][0],
                                        }
                                    yield {
                                        "type": "response.output_text.delta",
                                        "item_id": message_item_id,
                                        "output_index": output_indices.get(message_item_id, 0),
                                        "content_index": 0,
                                        "delta": clean,
                                    }
                            in_think = True
                            buffer = buffer[idx + len(THINK_OPEN):]
                            continue

                        safe, keep = _safe_flush(buffer)
                        if safe:
                            clean = _clean_response_text(safe)
                            if clean:
                                text_parts.append(clean)
                                if not content_started:
                                    content_started = True
                                    item = _response_text_item("", message_item_id)
                                    oi, start_evt = _start_output_item(item)
                                    if start_evt:
                                        yield start_evt
                                    yield {
                                        "type": "response.content_part.added",
                                        "item_id": message_item_id,
                                        "output_index": oi,
                                        "content_index": 0,
                                        "part": item["content"][0],
                                    }
                                yield {
                                    "type": "response.output_text.delta",
                                    "item_id": message_item_id,
                                    "output_index": output_indices.get(message_item_id, 0),
                                    "content_index": 0,
                                    "delta": clean,
                                }
                        buffer = keep
                        break
                    else:
                        idx = buffer.find(THINK_CLOSE)
                        if idx != -1:
                            safe, keep = _safe_flush(buffer[:idx])
                            if safe:
                                reasoning_parts.append(safe)
                                item = _response_reasoning_item("", reasoning_item_id)
                                oi, start_evt = _start_output_item(item)
                                if start_evt:
                                    yield start_evt
                                yield {
                                    "type": "response.reasoning_text.delta",
                                    "item_id": reasoning_item_id,
                                    "output_index": output_indices.get(reasoning_item_id, 0),
                                    "content_index": 0,
                                    "delta": safe,
                                }
                            in_think = False
                            buffer = buffer[idx + len(THINK_CLOSE):]
                            continue

                        safe, keep = _safe_flush(buffer)
                        if safe:
                            reasoning_parts.append(safe)
                            if len(reasoning_parts) == 1:
                                item = _response_reasoning_item("", reasoning_item_id)
                                oi, start_evt = _start_output_item(item)
                                if start_evt:
                                    yield start_evt
                            yield {
                                "type": "response.reasoning_text.delta",
                                "item_id": reasoning_item_id,
                                "output_index": output_indices.get(reasoning_item_id, 0),
                                "content_index": 0,
                                "delta": safe,
                            }
                        buffer = keep
                        break

            # 发送剩余缓冲区内容
            if buffer:
                clean = _clean_response_text(buffer)
                if clean:
                    if in_think:
                        reasoning_parts.append(clean)
                        yield {
                            "type": "response.reasoning_text.delta",
                            "item_id": reasoning_item_id,
                            "output_index": output_indices.get(reasoning_item_id, 0),
                            "content_index": 0,
                            "delta": clean,
                        }
                    else:
                        text_parts.append(clean)
                        if not content_started:
                            content_started = True
                            item = _response_text_item("", message_item_id)
                            oi, start_evt = _start_output_item(item)
                            if start_evt:
                                yield start_evt
                            yield {
                                "type": "response.content_part.added",
                                "item_id": message_item_id,
                                "output_index": oi,
                                "content_index": 0,
                                "part": item["content"][0],
                            }
                        yield {
                            "type": "response.output_text.delta",
                            "item_id": message_item_id,
                            "output_index": output_indices.get(message_item_id, 0),
                            "content_index": 0,
                            "delta": clean,
                        }

        # ─── 完成事件 ─────────────────────────────────────
        # 构建 output items
        output_by_id: dict[str, dict] = {}
        if reasoning_parts:
            output_by_id[reasoning_item_id] = _response_reasoning_item("".join(reasoning_parts), reasoning_item_id)
        full_text = _normalize_structured_output_text("".join(text_parts), text_config) if text_parts else ""
        if full_text:
            output_by_id[message_item_id] = _response_text_item(full_text, message_item_id)
        for idx in sorted(tool_calls_map.keys()):
            tc = tool_calls_map[idx]
            output_by_id[tc["id"]] = {
                "id": tc["id"],
                "type": "function_call",
                "call_id": tc.get("call_id", tc["id"]),
                "name": tc["name"],
                "arguments": tc["arguments"],
                "status": "completed",
            }

        if not output_by_id:
            output_by_id[message_item_id] = _response_text_item("", message_item_id)
            if message_item_id not in output_indices:
                item = _response_text_item("", message_item_id)
                oi, start_evt = _start_output_item(item)
                if start_evt:
                    yield start_evt

        output = [
            item for _, item in sorted(
                output_by_id.items(),
                key=lambda pair: output_indices.get(pair[0], len(output_indices))
            )
        ]

        # 发出 done 事件
        if reasoning_parts:
            yield {
                "type": "response.reasoning_text.done",
                "item_id": reasoning_item_id,
                "output_index": output_indices.get(reasoning_item_id, 0),
                "content_index": 0,
                "text": "".join(reasoning_parts),
            }
        if full_text:
            yield {
                "type": "response.output_text.done",
                "item_id": message_item_id,
                "output_index": output_indices.get(message_item_id, 0),
                "content_index": 0,
                "text": full_text,
            }
            yield {
                "type": "response.content_part.done",
                "item_id": message_item_id,
                "output_index": output_indices.get(message_item_id, 0),
                "content_index": 0,
                "part": _response_text_item(full_text, message_item_id)["content"][0],
            }
        for idx in sorted(tool_calls_map.keys()):
            tc = tool_calls_map[idx]
            yield {
                "type": "response.function_call_arguments.done",
                "item_id": tc["id"],
                "output_index": output_indices.get(tc["id"], 0),
                "arguments": tc["arguments"],
            }
        for idx, item in enumerate(output):
            if item.get("type") == "reasoning":
                # 由 reasoning_text.done 结束，不发 output_item.done
                # 避免 RikkaHub 重复创建空白思维链卡片
                continue
            yield {
                "type": "response.output_item.done",
                "output_index": idx,
                "item": item,
            }

        # 用量统计 — 优先使用API返回的准确数据，否则估算
        if api_usage:
            completion_record = {
                "input_tokens": api_usage.get("promptTokens", 0),
                "output_tokens": api_usage.get("completionTokens", 0),
                "total_tokens": api_usage.get("totalTokens", 0) or (api_usage.get("promptTokens", 0) + api_usage.get("completionTokens", 0)),
            }
        else:
            total_reasoning = "".join(reasoning_parts)
            total_text = "".join(text_parts)
            approx_completion = _count_tokens(total_reasoning + total_text)
            approx_prompt = _count_tokens(query)
            completion_record = {
                "input_tokens": approx_prompt,
                "output_tokens": approx_completion,
                "total_tokens": approx_prompt + approx_completion,
            }

        completed_payload = dict(init_payload)
        completed_payload["status"] = "completed"
        completed_payload["completed_at"] = int(time.time())
        completed_payload["output"] = output
        completed_payload["usage"] = _build_response_usage(completion_record)
        if has_tools and tool_calls_map:
            completed_payload["status"] = "completed"
        yield {"type": "response.completed", "response": completed_payload}

    except WorkBuddyApiError as e:
        failed_payload = dict(init_payload)
        failed_payload["status"] = "failed"
        failed_payload["error"] = {"message": f"WorkBuddy API {e.status_code}: {e.response_body[:200]}", "type": "server_error"}
        yield {"type": "response.failed", "response": failed_payload}
    except httpx.ReadTimeout:
        failed_payload = dict(init_payload)
        failed_payload["status"] = "incomplete"
        failed_payload["incomplete_details"] = {"reason": "max_output_tokens"}
        yield {"type": "response.incomplete", "response": failed_payload}
    except Exception as e:
        failed_payload = dict(init_payload)
        failed_payload["status"] = "failed"
        failed_payload["error"] = {"message": str(e)[:500], "type": "server_error"}
        yield {"type": "response.failed", "response": failed_payload}


async def _sse_stream_response(body: dict, account):
    """将 _stream_response_events 包装为 SSE 格式。"""
    async for event in _stream_response_events(body, account):
        yield _sse_json(event)


# ─── 路由：8 个 Responses API 端点 ──────────────────────────

@router.post("/v1/responses")
async def create_response(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
):
    """创建 Response（非流式/流式）。"""
    api_key = authorization or (f"Bearer {x_api_key}" if x_api_key else None)
    if not validate_api_key(api_key):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key"}})

    body = await request.json()
    stream = body.get("stream", False)
    body["_response_id"] = _gen_response_id()

    account = config_manager.get_next_account()
    if not account:
        raise HTTPException(status_code=503, detail={"error": {"message": "no workbuddy account"}})

    if stream:
        return StreamingResponse(
            _sse_stream_response(body, account),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            }
        )

    # 非流式
    try:
        model_used, usage, items = await _do_response_chat(body, account)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": {"message": str(e)}})

    response_id = body["_response_id"]
    response_obj = _make_response_object(
        response_id=response_id,
        model=model_used,
        status="completed",
        items=items,
        usage=usage,
    )

    # 保存记录
    record = dict(response_obj)
    record["_messages"] = _convert_response_input_to_messages(body.get("input", []))
    record["_input"] = body.get("input", [])
    record["_body"] = body
    try:
        _save_response_record(record)
    except Exception:
        pass

    # 记录用量
    if usage:
        _add_usage(model_used, usage.get("promptTokens", 0) or usage.get("prompt_tokens", 0),
                   usage.get("completionTokens", 0) or usage.get("completion_tokens", 0))

    return response_obj


@router.post("/v1/responses/input_tokens")
async def count_input_tokens(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
):
    """估算 input tokens。"""
    api_key = authorization or (f"Bearer {x_api_key}" if x_api_key else None)
    if not validate_api_key(api_key):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key"}})

    body = await request.json()
    input_value = body.get("input")
    instructions = body.get("instructions") if isinstance(body.get("instructions"), str) else None
    tools = body.get("tools") if isinstance(body.get("tools"), list) else None
    count = _count_response_input_tokens(input_value, instructions, tools)
    return {"object": "response.input_tokens", "input_tokens": count}


def _compact_response_record(source: dict, body: dict) -> dict:
    """创建 compacted response 记录。"""
    response_id = _gen_response_id()
    now = int(time.time())
    source_text = _extract_output_text(source.get("output", []))
    compact_text = body.get("summary") if isinstance(body.get("summary"), str) else source_text
    compact_item = _response_text_item(compact_text or "", response_id)
    compact_messages = [{"role": "assistant", "content": compact_text or ""}]
    compact_body = {
        "_response_id": response_id,
        "input": [compact_item],
        "model": body.get("model") or source.get("model", "default"),
        "previous_response_id": source.get("id"),
        "metadata": dict(source.get("metadata") or {}),
        "store": True,
    }
    compact_body["metadata"].update({
        "compacted": True,
        "source_response_id": source.get("id"),
    })
    record = _make_response_object(
        response_id=response_id,
        model=compact_body["model"],
        status="completed",
        items=[_response_text_item(compact_text or "")],
        usage={
            "prompt_tokens": _count_tokens(json.dumps(source.get("_input", []), ensure_ascii=False)),
            "completion_tokens": _count_tokens(compact_text or ""),
            "total_tokens": _count_tokens(json.dumps(source.get("_input", []), ensure_ascii=False)) + _count_tokens(compact_text or ""),
        },
    )
    record["_messages"] = compact_messages
    record["_input"] = compact_body["input"]
    record["_body"] = compact_body
    record["store"] = True
    return record


@router.post("/v1/responses/compact")
async def compact_response(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
):
    """压缩 response。"""
    api_key = authorization or (f"Bearer {x_api_key}" if x_api_key else None)
    if not validate_api_key(api_key):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key"}})

    body = await request.json()
    response_id = body.get("response_id") or body.get("previous_response_id")
    if not response_id:
        raise HTTPException(status_code=400, detail={"error": {"message": "response_id is required", "type": "invalid_request_error"}})
    source = _get_response_record(response_id)
    if not source:
        raise HTTPException(status_code=404, detail={"error": {"message": f"response {response_id} not found", "type": "invalid_request_error"}})
    record = _compact_response_record(source, body)
    _save_response_record(record)
    return record


@router.post("/v1/responses/{response_id}/compact")
async def compact_response_by_id(
    response_id: str,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
):
    """按 ID 压缩 response。"""
    api_key = authorization or (f"Bearer {x_api_key}" if x_api_key else None)
    if not validate_api_key(api_key):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key"}})

    body = await request.json()
    source = _get_response_record(response_id)
    if not source:
        raise HTTPException(status_code=404, detail={"error": {"message": f"response {response_id} not found", "type": "invalid_request_error"}})
    record = _compact_response_record(source, body)
    _save_response_record(record)
    return record


@router.post("/v1/responses/{response_id}/cancel")
async def cancel_response(
    response_id: str,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
):
    """取消 response。"""
    api_key = authorization or (f"Bearer {x_api_key}" if x_api_key else None)
    if not validate_api_key(api_key):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key"}})

    record = _get_response_record(response_id)
    if not record:
        raise HTTPException(status_code=404, detail={"error": {"message": f"response {response_id} not found", "type": "invalid_request_error"}})
    if record.get("status") == "cancelled":
        return record
    if record.get("status") in _RESPONSE_TERMINAL_STATUSES:
        return record

    now = int(time.time())
    record["status"] = "cancelled"
    record["completed_at"] = now
    record["error"] = None
    record["incomplete_details"] = None
    _update_response_record(response_id, record)
    return record


@router.get("/v1/responses/{response_id}")
async def get_response(
    response_id: str,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
):
    """获取 response 记录。"""
    api_key = authorization or (f"Bearer {x_api_key}" if x_api_key else None)
    if not validate_api_key(api_key):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key"}})

    record = _get_response_record(response_id)
    if not record:
        raise HTTPException(status_code=404, detail={"error": {"message": f"response {response_id} not found", "type": "invalid_request_error"}})
    return record


@router.get("/v1/responses/{response_id}/input_items")
async def get_response_input_items(
    response_id: str,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
):
    """获取 response 的 input items。"""
    api_key = authorization or (f"Bearer {x_api_key}" if x_api_key else None)
    if not validate_api_key(api_key):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key"}})

    record = _get_response_record(response_id)
    if not record:
        raise HTTPException(status_code=404, detail={"error": {"message": f"response {response_id} not found", "type": "invalid_request_error"}})

    stored_items = record.get("_input") or record.get("input") or []
    if not isinstance(stored_items, list):
        stored_items = [stored_items] if stored_items else []

    # 统一转换为 dict 格式（input 可能是纯字符串）
    normalized = []
    for item in stored_items:
        if isinstance(item, str):
            normalized.append({"id": f"inp_{uuid.uuid4().hex[:24]}", "type": "input_text", "text": item})
        elif isinstance(item, dict):
            normalized.append(item)
    stored_items = normalized

    limit_raw = request.query_params.get("limit")
    try:
        limit = max(1, min(int(limit_raw), 100)) if limit_raw is not None else 20
    except ValueError:
        raise HTTPException(status_code=400, detail={"error": {"message": "invalid limit", "type": "invalid_request_error"}})

    after = request.query_params.get("after")
    before = request.query_params.get("before")
    order = (request.query_params.get("order") or "desc").lower()
    if order not in ("asc", "desc"):
        raise HTTPException(status_code=400, detail={"error": {"message": "invalid order", "type": "invalid_request_error"}})

    # 简单分页
    ordered = list(stored_items)
    if order == "desc":
        ordered = list(reversed(ordered))
    if after:
        idx = next((i for i, item in enumerate(ordered) if item.get("id") == after), -1)
        ordered = ordered[idx + 1:] if idx != -1 else []
    if before:
        idx = next((i for i, item in enumerate(ordered) if item.get("id") == before), -1)
        ordered = ordered[:idx] if idx != -1 else []

    has_more = len(ordered) > limit
    page_items = ordered[:limit]

    return {
        "object": "list",
        "data": page_items,
        "first_id": page_items[0].get("id") if page_items else None,
        "last_id": page_items[-1].get("id") if page_items else None,
        "has_more": has_more,
    }


@router.delete("/v1/responses/{response_id}")
async def delete_response(
    response_id: str,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
):
    """删除 response 记录。"""
    api_key = authorization or (f"Bearer {x_api_key}" if x_api_key else None)
    if not validate_api_key(api_key):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key"}})

    if not _delete_response_record(response_id):
        raise HTTPException(status_code=404, detail={"error": {"message": f"response {response_id} not found", "type": "invalid_request_error"}})
    return {"id": response_id, "object": "response", "deleted": True}

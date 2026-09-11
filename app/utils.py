"""工具函数 — WorkBuddy Desktop-api

凭证解析、媒体提取/内联、消息构建。
"""

import re
import json as _json
from typing import Optional, List, Tuple, Dict, Any
from .config import WorkBuddyAccount


def parse_curl(curl_command: str) -> Optional[WorkBuddyAccount]:
    """解析 cURL 命令提取 WorkBuddy 账号凭证。

    识别 `Authorization: Bearer <jwt>`（或 `-H "authorization: bearer ..."`）
    与 `X-User-Id: <uid>` 两种头部。
    """
    account = {
        "wb_access_token": "",
        "wb_uid": "",
        "wb_refresh_token": "",
    }

    token_patterns = [
        r'-\s*[Hh]\s+[\'"][Aa]uthorization:\s*Bearer\s+([A-Za-z0-9._\-]+)[\'"]',
        r'-\s*[Hh]\s+[\'"][Aa]uthorization:\s*([A-Za-z0-9._\-]{20,})[\'"]',
        r'Bearer\s+([A-Za-z0-9._\-]{20,})',
    ]
    for pat in token_patterns:
        m = re.search(pat, curl_command)
        if m:
            account["wb_access_token"] = m.group(1)
            break

    uid_match = re.search(
        r'-\s*[Hh]\s+[\'"]X-User-Id:\s*([A-Za-z0-9._\-]+)[\'"]', curl_command, re.I
    )
    if uid_match:
        account["wb_uid"] = uid_match.group(1)

    if not account["wb_access_token"]:
        return None

    return WorkBuddyAccount(**account, uid=account["wb_uid"])


def extract_medias_from_messages(messages: list) -> Tuple[str, list, list, list]:
    """从消息列表中提取图片/视频/音频媒体和文本文件。

    Returns:
        (query_text, base64_medias, text_files, processed_messages)
        text_files: [{"base64": ..., "filename": ..., "mimeType": ...}, ...]
    """
    base64_medias = []
    text_files = []
    seen_base64 = set()
    processed_messages = []

    for msg in messages:
        text = ""
        content = msg.content or ""

        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    text += item.get("text", "")
                elif item.get("type") == "image_url":
                    img_url = item.get("image_url", {})
                    url = img_url.get("url", "") if isinstance(img_url, dict) else str(img_url)
                    if url and url.startswith("data:"):
                        base64 = url.split(",", 1)[1] if "," in url else url
                        if base64 and base64 not in seen_base64:
                            mime = url.split(";")[0].split(":")[1] if ";" in url else "image/jpeg"
                            base64_medias.append({
                                "base64": base64,
                                "mimeType": mime,
                                "type": "image"
                            })
                            seen_base64.add(base64)
                elif item.get("type") == "file":
                    # 文本文件：收集 base64 用于 WorkBuddy 上传（mediaType="file"）
                    file_obj = item.get("file", {})
                    if isinstance(file_obj, dict):
                        filename = file_obj.get("filename", "file.txt")
                        file_data = file_obj.get("file_data", "") or file_obj.get("data", "")
                        if file_data and file_data not in seen_base64:
                            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "txt"
                            text_files.append({
                                "base64": file_data,
                                "filename": filename,
                                "mimeType": "text/plain"
                            })
                            seen_base64.add(file_data)
        else:
            text = str(content) if content else ""

        if hasattr(msg, 'tool_calls') and msg.tool_calls:
            text = _serialize_tool_calls(msg.tool_calls)

        if msg.role == "tool":
            tool_call_id = getattr(msg, 'tool_call_id', '') or ''
            clean = re.sub(r'\[TOOL_RESULT\]\s*', '', text, flags=re.IGNORECASE)
            text = f"[tool_result id={tool_call_id[:8]}] {clean}"

        processed_messages.append({"role": msg.role, "text": text})

    query_text = processed_messages[-1]["text"] if processed_messages else ""
    return query_text, base64_medias, text_files, processed_messages


def _serialize_tool_calls(tool_calls: list) -> str:
    """统一定义工具调用序列化 — 兼容 dict 和 pydantic model。"""
    tc_lines = []
    for tc in tool_calls:
        fn = _safe_nested_get(tc, "function")
        if not fn:
            continue
        fname = _safe_nested_get(fn, "name", "")
        args_str = _safe_nested_get(fn, "arguments", "{}")

        try:
            args = _json.loads(args_str) if isinstance(args_str, str) else args_str
            if isinstance(args, dict):
                kv = ", ".join(f"{k}={v!r}" for k, v in args.items())
            else:
                kv = str(args)
        except Exception:
            kv = str(args_str)

        tc_lines.append(f"TOOL_CALL: {fname}({kv})")

    return "\n".join(tc_lines)


def _safe_nested_get(obj, *keys, default=None):
    """安全嵌套取值 — 兼容 dict 和 pydantic model。"""
    for key in keys:
        if obj is None:
            return default
        if isinstance(obj, dict):
            obj = obj.get(key, default)
        else:
            obj = getattr(obj, key, default)
    return obj


# ── 附件处理（内联，无上传） ──────────────────────────────
#
# WorkBuddy 上游（copilot.tencent.com/v2/chat/completions）是 OpenAI 兼容接口，
# 没有任何公开的文件上传接口（旧代码里的 /open-apis/resource/* 是上游不存在的地址，
# 实测返回连接失败/404）。因此附件一律内联进用户消息：
#   - 图片 → image_url + base64 data URL
#     （实测 auto / hy4-preview / glm-5.3 / kimi-k2.6 均可正确识图）
#   - 文本文件 → 独立的 text 内容块（由 workbuddy_client._query_body 展开）
# 两个函数保留原名与 async 签名，既有调用点无需改动。

MAX_INLINE_FILE_CHARS = 200_000


def _strip_data_url(data: str) -> str:
    """去掉 `data:...;base64,` 前缀，返回裸 base64。"""
    if not data:
        return ""
    s = data.strip()
    if s.startswith("data:") and "," in s:
        return s.split(",", 1)[1]
    return s


async def upload_media_to_workbuddy(
    base64_data: str,
    mime_type: str,
    account: WorkBuddyAccount,
    model: str = "hy3",
) -> Optional[Dict[str, Any]]:
    """把图片转成内联的 OpenAI `image_url` 内容块（不做任何网络请求）。

    返回的 dict 带 `url`（data URL），由 `_query_body` 组装进请求体。
    """
    data = _strip_data_url(base64_data)
    if not data:
        return None
    mime = mime_type or "image/png"
    return {
        "mediaType": "image",
        "type": "image_url",
        "url": f"data:{mime};base64,{data}",
    }


def _decode_text_file(base64_data: str) -> str:
    """base64 → 文本，依次尝试 utf-8 / gb18030 / latin-1。"""
    data = _strip_data_url(base64_data)
    if not data:
        return ""
    import base64 as _b64

    try:
        raw = _b64.b64decode(data)
    except Exception:
        return ""
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


async def upload_text_file_to_workbuddy(
    base64_data: str,
    filename: str,
    mime_type: str,
    account: WorkBuddyAccount,
    model: str = "hy4-preview",
) -> Optional[Dict[str, Any]]:
    """把文本文件内联成文本内容块（不做任何网络请求）。

    内容作为独立 text 块附在用户消息后，由 `_query_body` 展开。
    """
    text = _decode_text_file(base64_data)
    if not text.strip():
        return None
    if len(text) > MAX_INLINE_FILE_CHARS:
        text = text[:MAX_INLINE_FILE_CHARS] + "\n...(文件过长已截断)"
    return {
        "mediaType": "file",
        "type": "file",
        "name": filename or "file.txt",
        "text": text,
    }


def build_query_from_messages(
    messages: list,
    tools: list = None,
    passthrough: bool = False,
    continuation: bool = False,
    no_truncate: bool = False,
) -> str:
    """从消息列表构建查询字符串。

    格式：系统消息（含工具提示词）→ 对话历史。
    WorkBuddy API 没有 system/user 角色分离，query 是纯文本拼接。
    工具提示词嵌入 system 消息一次，不再每轮重复注入。
    无 system 消息但有 tools 时自动补 system。
    passthrough=True 时跳过 WorkBuddyML 格式说明书，直接嵌入原始工具定义。

    continuation=True 时只发 system + tools + 最后一条 user 消息，
    跳过历史对话（WorkBuddy 服务端通过 conversationId 已有上下文）。
    """
    from .tool_call import build_tool_prompt

    query_parts = []
    system_text = ""

    # 分离 system 消息和其他消息
    non_system_msgs = []
    for msg in messages:
        role = msg.role
        content = msg.content or ""

        if role == "system":
            if isinstance(content, list):
                text_parts = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text_parts.append(item.get("text", ""))
                content = " ".join(text_parts)
            system_text = str(content).strip()
            continue

        non_system_msgs.append(msg)

    # continuation 模式：只取最后一条 user 消息
    if continuation and non_system_msgs:
        last_user = None
        for msg in reversed(non_system_msgs):
            if msg.role == "user":
                last_user = msg
                break
        if last_user:
            non_system_msgs = [last_user]

    for msg in non_system_msgs:
        role = msg.role
        content = msg.content or ""

        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(item.get("text", ""))
            content = " ".join(text_parts)

        if hasattr(msg, 'tool_calls') and msg.tool_calls:
            content = _serialize_tool_calls(msg.tool_calls)

        if role == "tool":
            tool_call_id = getattr(msg, 'tool_call_id', '') or ''
            clean = re.sub(r'\[TOOL_RESULT\]\s*', '', content, flags=re.IGNORECASE)
            clean = clean.strip()
            content = f"[tool_result id={tool_call_id[:8]}] {clean}"

        query_parts.append(f"{role}: {content}")

    # 工具提示词嵌入 system 消息（一次，不再每轮重复追加末尾）
    if tools:
        tool_prompt = build_tool_prompt(tools, passthrough=passthrough)
        if tool_prompt:
            if system_text:
                system_text = system_text + "\n\n" + tool_prompt
            else:
                system_text = tool_prompt

    # system 消息插入最前面
    if system_text:
        query_parts.insert(0, f"system: {system_text}")

    full_query = "\n".join(query_parts)

    # === 长度保护：v2.5/v2.5-pro 上下文窗口 1M，实测单条 query 1M 字符仍 200（无 ~100KB 硬限）；
    # 老模型（v2-flash 等）若服务端拒长文本，可调小 WORKBUDDY_MAX_QUERY_CHARS 环境变量覆盖。
    # continuation 模式下只发增量，通常远低于限制。
    # 非 continuation 模式下采用滑动窗口：保留 system，从尾部裁剪历史。
    MAX_QUERY_CHARS = int(__import__("os").getenv("WORKBUDDY_MAX_QUERY_CHARS", "102000"))
    if not no_truncate and len(full_query) > MAX_QUERY_CHARS:
        system_prefix = ""
        history_parts = query_parts
        if system_text:
            system_prefix = f"system: {system_text}\n"
            history_parts = query_parts[1:]  # 去掉 system 行

        kept = []
        used = len(system_prefix)
        for part in reversed(history_parts):
            part_len = len(part) + 1  # +1 是换行符
            if used + part_len > MAX_QUERY_CHARS and kept:
                break
            kept.insert(0, part)
            used += part_len

        full_query = system_prefix + "\n".join(kept)
        print(
            f"[QueryGuard] WorkBuddy query exceeded {MAX_QUERY_CHARS} chars, "
            f"kept system + last {len(kept)} history parts "
            f"(final {len(full_query)} chars). Older history was truncated."
        )

    return full_query

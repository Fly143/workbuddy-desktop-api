"""WorkBuddy Desktop 会话客户端

唯一上游：copilot.tencent.com /v2/chat/completions
鉴权：本机 Desktop 凭证文件里的 accessToken（见 workbuddy_session.py）

注意：上游**只接受 stream=true**，非流式请求返回
`11101 Non-stream chat request is currently not supported`。
因此本层的非流式入口（chat_completion_json / call_api）统一走流式再聚合。

上游返回 OpenAI 兼容 SSE，本层只做鉴权、模型名透传、错误与 401 重试。
"""

from __future__ import annotations

import json
from typing import AsyncIterator, Optional, Tuple

import httpx

from .config import WorkBuddyAccount
from .workbuddy_session import (
    API_BASE,
    build_headers,
    get_session,
    invalidate_session_cache,
    list_remote_models,
)

TIMEOUT = 180.0
# 不要替用户塞默认 max_tokens。
# 实测（hy3）：max_tokens 是「reasoning + 正文」的合计预算——
#   - 不传：上游不限，模型充分思考后正常输出正文（写 3000 字长文正常推进）
#   - 传 4096：预算被 reasoning 全部吃光，finish_reason=length、正文 0 字符
# 所以代理层猜测一个值极易适得其反，未显式指定时保持透传，由上游/调用方决定。
THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"

# 上游未返回可用模型时的兜底列表
BUILTIN_MODELS = [
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


class WorkBuddyApiError(Exception):
    def __init__(self, status_code: int, response_body: str):
        self.status_code = status_code
        self.response_body = response_body
        super().__init__(f"WorkBuddy API error {status_code}: {response_body[:200]}")


def bare_model(model: str) -> str:
    s = str(model or "")
    i = s.find("/")
    return s[i + 1:] if i >= 0 else s


class WorkBuddyClient:
    """WorkBuddy Desktop 账号会话客户端"""

    def __init__(self, account: WorkBuddyAccount):
        self.account = account

    def _credentials(self) -> dict:
        return {
            "wb_access_token": self.account.wb_access_token or None,
            "wb_refresh_token": self.account.wb_refresh_token or None,
            "wb_uid": self.account.wb_uid or None,
        }

    async def _headers(self, stream: bool, client: httpx.AsyncClient) -> dict:
        session = await get_session(self._credentials(), client)
        if not session:
            raise WorkBuddyApiError(
                401,
                "WorkBuddy Desktop session unavailable. "
                "Sign in to WorkBuddy Desktop once (or import a token), then retry.",
            )
        return build_headers(session, stream=stream)

    def _url(self) -> str:
        return f"{API_BASE}/v2/chat/completions"

    def _prepare_body(self, body: dict, stream: bool) -> dict:
        out = dict(body)
        out["model"] = bare_model(out.get("model", ""))
        # 上游只支持流式，非流式由本层聚合
        out["stream"] = True
        if stream:
            out["stream_options"] = {"include_usage": True}
        return out

    # ── 请求 ──────────────────────────────────────────────

    async def chat_completion(
        self,
        body: dict,
        stream: bool = False,
        client: Optional[httpx.AsyncClient] = None,
    ) -> httpx.Response:
        """发起请求。stream=False 时返回一个已读完的伪响应（见 _collect_stream）。"""
        if not stream:
            return await self._collect_stream(body)

        url = self._url()
        payload = self._prepare_body(body, stream=True)

        owns = client is None
        if owns:
            client = httpx.AsyncClient(timeout=TIMEOUT)
        try:
            headers = await self._headers(True, client)

            async def _send(hdrs: dict) -> httpx.Response:
                req = client.build_request("POST", url, headers=hdrs, json=payload)
                return await client.send(req, stream=True)

            response = await _send(headers)

            if response.status_code in (401, 403):
                invalidate_session_cache(self.account.wb_access_token or None)
                await response.aclose()
                headers = await self._headers(True, client)
                response = await _send(headers)

            if response.status_code >= 400:
                err = (await response.aread()).decode("utf-8", "replace")
                await response.aclose()
                raise WorkBuddyApiError(response.status_code, err)

            return response
        except Exception:
            if owns:
                await client.aclose()
            raise

    async def _collect_stream(self, body: dict) -> httpx.Response:
        """走流式上游，聚合成一个 OpenAI 非流式响应。"""
        payload = self._prepare_body(body, stream=False)
        acc = _StreamAccumulator()
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            headers = await self._headers(True, client)
            async with client.stream(
                "POST", self._url(), headers=headers, json=payload
            ) as resp:
                if resp.status_code in (401, 403):
                    invalidate_session_cache(self.account.wb_access_token or None)
                    headers = await self._headers(True, client)
                    resp = await client.send(
                        client.build_request(
                            "POST", self._url(), headers=headers, json=payload
                        ),
                        stream=True,
                    )
                if resp.status_code >= 400:
                    err = (await resp.aread()).decode("utf-8", "replace")
                    raise WorkBuddyApiError(resp.status_code, err)
                async for line in resp.aiter_lines():
                    acc.feed(line)

        return _fake_response(acc.to_completion())

    async def chat_completion_json(self, body: dict) -> dict:
        resp = await self.chat_completion(body, stream=False)
        return resp.json()

    async def chat_completion_stream(self, body: dict) -> AsyncIterator[bytes]:
        client = httpx.AsyncClient(timeout=TIMEOUT)
        try:
            resp = await self.chat_completion(body, stream=True, client=client)
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await client.aclose()

    async def list_models(self) -> list[str]:
        """优先云端列表，失败回退内置列表。"""
        remote = await list_remote_models(self._credentials())
        return remote or list(BUILTIN_MODELS)

    async def test_connection(self) -> Tuple[bool, str]:
        """探活：打一次最小流式请求。
        按用户\"全部移除限制\"指令：不填 max_tokens，让上游/模型自行决定输出长度。"""
        if not self.account.has_session():
            return False, "no accessToken configured"
        try:
            chunks: list[dict] = []
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                headers = await self._headers(True, client)
                payload = self._prepare_body(
                    {
                        "model": bare_model(BUILTIN_MODELS[1]),
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                    stream=True,
                )
                async with client.stream(
                    "POST", self._url(), headers=headers, json=payload
                ) as resp:
                    if resp.status_code >= 400:
                        return False, f"HTTP {resp.status_code}"
                    async for line in resp.aiter_lines():
                        if line.startswith("data:"):
                            chunks.append({"raw": line})
            return True, "session ok"
        except WorkBuddyApiError as e:
            return False, f"HTTP {e.status_code}: {e.response_body[:80]}"
        except Exception as e:
            return False, str(e)[:120]

    # ── 兼容 anthropic_routes / routes 的旧接口 ──────────────

    @staticmethod
    def _normalize_tools(tools: list | None) -> list | None:
        """规范化为 Chat Completions 的 {type, function:{name,...}} 格式。

        Responses API 的 tools 是扁平的（name/parameters 在顶层），
        直接透传会让上游报 `function' is null`。
        非 function 类型（web_search / computer_use 等）丢弃。
        """
        if not tools:
            return None
        out = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            fn = t.get("function")
            if isinstance(fn, dict) and fn.get("name"):
                out.append({
                    "type": "function",
                    "function": {
                        "name": fn["name"],
                        "description": fn.get("description") or "",
                        "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
                    },
                })
                continue
            if t.get("name") and (t.get("type") in (None, "function")):
                out.append({
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description") or "",
                        "parameters": t.get("parameters") or {"type": "object", "properties": {}},
                    },
                })
        return out or None

    def _query_body(
        self,
        query: str,
        thinking: bool,
        model: str,
        multi_medias: list | None = None,
        attachments: list | None = None,
        tools: list | None = None,
        stream: bool = False,
    ) -> dict:
        parts: list = [{"type": "text", "text": query}]
        for m in multi_medias or []:
            if not isinstance(m, dict):
                continue
            # 文本文件：内联为独立 text 块（上游无文件上传接口）
            if (m.get("mediaType") or m.get("type")) == "file":
                text = m.get("text") or ""
                if text:
                    name = m.get("name") or "file"
                    parts.append({
                        "type": "text",
                        "text": f'<file name="{name}">\n{text}\n</file>',
                    })
                continue
            # 图片：内联 image_url（base64 data URL 或远程 URL）
            url = m.get("url") or m.get("image_url")
            if url:
                parts.append({"type": "image_url", "image_url": {"url": url}})
        content = parts if len(parts) > 1 else query
        body = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "stream": stream,
        }
        if thinking:
            body["reasoning_effort"] = "medium"
        norm_tools = self._normalize_tools(tools)
        if norm_tools:
            body["tools"] = norm_tools
            body["tool_choice"] = "auto"
        return body

    @staticmethod
    def _split_think_from_content(content: str) -> tuple[str, str]:
        open_tag, close_tag = "<think>", "</think>"
        if open_tag not in content:
            return content, ""
        main, think, in_think, buf, i = [], [], False, "", 0
        while i < len(content):
            if not in_think and content.startswith(open_tag, i):
                in_think = True
                i += len(open_tag)
                continue
            if in_think and content.startswith(close_tag, i):
                in_think = False
                think.append(buf)
                buf = ""
                i += len(close_tag)
                continue
            buf += content[i]
            i += 1
        if in_think:
            think.append(buf)
        else:
            main.append(buf)
        return "".join(main), "\n".join(think)

    @staticmethod
    def _native_tool_calls_to_text(tool_calls: list) -> str:
        """OpenAI message.tool_calls → 文本，供 extract_tool_call / StreamSieve 解析。"""
        lines = []
        for tc in tool_calls or []:
            fn = (tc or {}).get("function") or {}
            name = fn.get("name") or ""
            args = fn.get("arguments") or "{}"
            if not name:
                continue
            lines.append(f"TOOL_CALL: {name}({args})")
        return "\n".join(lines)

    @staticmethod
    def _merge_stream_tool_calls(acc: dict, deltas: list | None) -> list:
        """合并 SSE 里分片的 delta.tool_calls，按 index 聚合。"""
        for d in deltas or []:
            idx = d.get("index", 0)
            slot = acc.setdefault(idx, {"id": None, "function": {"name": "", "arguments": ""}})
            if d.get("id"):
                slot["id"] = d["id"]
            fn = d.get("function") or {}
            if fn.get("name"):
                slot["function"]["name"] += fn["name"]
            if fn.get("arguments"):
                slot["function"]["arguments"] += fn["arguments"]
        return [acc[i] for i in sorted(acc)]

    async def call_api(
        self, query: str, thinking: bool = False, model: str = "hy3",
        multi_medias: list | None = None, attachments: list | None = None,
        conversation_id: str | None = None, tools: list | None = None,
    ) -> Tuple[str, str, dict, list]:
        body = self._query_body(query, thinking, model, multi_medias, attachments, tools=tools)
        data = await self.chat_completion_json(body)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        raw = message.get("content") or ""
        content, think = self._split_think_from_content(raw)

        reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
        if reasoning:
            think = (think + "\n" if think else "") + reasoning

        native_tc = message.get("tool_calls") or []
        if native_tc:
            tc_text = self._native_tool_calls_to_text(native_tc)
            content = (content + "\n" if content else "") + tc_text

        usage = data.get("usage") or {}
        return content, think, {
            "promptTokens": usage.get("prompt_tokens") or 0,
            "completionTokens": usage.get("completion_tokens") or 0,
        }, []

    async def stream_api(
        self, query: str, thinking: bool = False, model: str = "hy3",
        multi_medias: list | None = None, attachments: list | None = None,
        conversation_id: str | None = None, tools: list | None = None,
    ) -> AsyncIterator[dict]:
        body = self._query_body(query, thinking, model, multi_medias, attachments, tools=tools, stream=True)
        body["stream_options"] = {"include_usage": True}
        client = httpx.AsyncClient(timeout=TIMEOUT)
        tc_acc: dict = {}
        try:
            resp = await self.chat_completion(body, stream=True, client=client)
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    merged = self._merge_stream_tool_calls(tc_acc, None)
                    if merged:
                        text = self._native_tool_calls_to_text(merged)
                        if text:
                            yield {"type": "text", "content": "\n" + text + "\n"}
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage"):
                    u = chunk["usage"]
                    yield {
                        "type": "usage",
                        "promptTokens": u.get("prompt_tokens") or 0,
                        "completionTokens": u.get("completion_tokens") or 0,
                    }
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        yield {"type": "text", "content": delta["content"]}
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                    if reasoning:
                        yield {"type": "text", "content": THINK_OPEN + reasoning + THINK_CLOSE}
                    if delta.get("tool_calls"):
                        self._merge_stream_tool_calls(tc_acc, delta["tool_calls"])
        finally:
            await client.aclose()


# ── 流式 → 非流式聚合 ─────────────────────────────────────


class _StreamAccumulator:
    """把 OpenAI SSE 增量聚合成一条完整 message。"""

    def __init__(self) -> None:
        self.id = ""
        self.model = ""
        self.created = 0
        self.content = ""
        self.reasoning = ""
        self.finish_reason: Optional[str] = None
        self.usage: dict = {}
        self._tc: dict = {}

    def feed(self, line: str) -> None:
        if not line.startswith("data:"):
            return
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            return
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            return
        if not self.id:
            self.id = chunk.get("id") or ""
        if not self.model:
            self.model = chunk.get("model") or ""
        if not self.created:
            self.created = chunk.get("created") or 0
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        WorkBuddyClient._merge_stream_tool_calls(self._tc, None)
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            self.content += delta.get("content") or ""
            self.reasoning += delta.get("reasoning_content") or delta.get("reasoning") or ""
            if delta.get("tool_calls"):
                WorkBuddyClient._merge_stream_tool_calls(self._tc, delta["tool_calls"])
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]

    def to_completion(self) -> dict:
        message: dict = {"role": "assistant", "content": self.content}
        if self.reasoning:
            message["reasoning_content"] = self.reasoning
        merged = WorkBuddyClient._merge_stream_tool_calls(self._tc, None)
        if merged:
            message["tool_calls"] = merged
        return {
            "id": self.id,
            "object": "chat.completion",
            "model": self.model,
            "created": self.created,
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": self.finish_reason or "stop",
            }],
            "usage": self.usage or {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }


def _fake_response(payload: dict) -> httpx.Response:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return httpx.Response(
        200,
        content=body,
        headers={"Content-Type": "application/json"},
        request=httpx.Request("POST", "http://local/aggregate"),
    )

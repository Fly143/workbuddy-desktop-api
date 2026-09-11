"""WorkBuddy Desktop 账号会话

从本机 WorkBuddy Desktop 的共享凭证文件读取账号会话：
  %LOCALAPPDATA%/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info

该文件由 Desktop 主进程通过 FileAuthenticationStorage 落盘（UTF-8 JSON），
结构（节选）：
  {
    "account": {"uid": "...", "nickname": "...", "type": "personal", ...},
    "auth": {
      "accessToken":  "<JWT>",
      "refreshToken": "<JWT>",
      "tokenType": "Bearer",
      "domain": "www.codebuddy.cn",
      "expiresAt": <ms>, "refreshExpiresAt": <ms>
    },
    "accounts": [...], "allAccounts": [...]
  }

accessToken 过期前用 refreshToken 调 /v2/plugin/auth/token/refresh 续期。
上游网关从 User-Agent 解析客户端版本，缺失会返回
`check ua, get coding copilot version error`。

上游：https://copilot.tencent.com
  POST /v2/chat/completions             — OpenAI 兼容 SSE（仅流式）
  GET  /v3/config                       — 云端产品与模型配置
  GET  /v2/enterprises/personal/models  — 当前账号可用模型
  POST /v2/plugin/auth/token/refresh    — 续期
  GET  /v2/user/cloudagent/quota        — 配额
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

import httpx

API_BASE = "https://copilot.tencent.com"

# `%LOCALAPPDATA%/CodeBuddyExtension` 来自 FilePathServiceImpl：
#   win32  : <home>/AppData/Local/<DATA_DIR_NAME>
#   darwin : <home>/Library/Application Support/<DATA_DIR_NAME>
#   linux  : <home>/.local/share/<DATA_DIR_NAME>
DATA_DIR_NAME = "CodeBuddyExtension"
AUTH_FILE_NAME = "workbuddy-desktop.info"

# 服务端从 UA 解析客户端版本，缺失会 403
CLIENT_VERSION = "5.5.4"
API_UA = f"WorkBuddy/{CLIENT_VERSION}"
DEFAULT_DOMAIN = "www.codebuddy.cn"

# accessToken 提前多少毫秒视为过期
EXPIRE_SKEW_MS = 5 * 60 * 1000

# key = sha256(accessToken) → {"session": {...}, "at": ts}
_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()


# ── 本机凭证文件 ──────────────────────────────────────────


def auth_file_path() -> Optional[Path]:
    """本机 WorkBuddy Desktop 共享凭证文件路径（不存在返回 None）。"""
    home = Path.home()
    if os.name == "nt":
        base = home / "AppData" / "Local" / DATA_DIR_NAME
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support" / DATA_DIR_NAME
    else:
        base = home / ".local" / "share" / DATA_DIR_NAME
    p = base / "Data" / "Public" / "auth" / AUTH_FILE_NAME
    return p if p.exists() else None


def _pick_account(doc: dict) -> dict:
    """优先取 lastLogin 的账号，其次第一个。"""
    for key in ("account", "accounts", "allAccounts"):
        val = doc.get(key)
        if isinstance(val, dict) and val.get("uid"):
            return val
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict) and item.get("lastLogin"):
                    return item
            for item in val:
                if isinstance(item, dict) and item.get("uid"):
                    return item
    return {}


def read_local_session() -> Optional[dict]:
    """读取本机 Desktop 凭证文件，返回归一化会话。"""
    path = auth_file_path()
    if not path:
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    auth = doc.get("auth") if isinstance(doc.get("auth"), dict) else {}
    account = _pick_account(doc)
    access = (auth.get("accessToken") or "").strip()
    if not access:
        return None

    return {
        "accessToken": access,
        "refreshToken": (auth.get("refreshToken") or "").strip(),
        "uid": account.get("uid") or "",
        "nickname": account.get("nickname") or "",
        "domain": auth.get("domain") or DEFAULT_DOMAIN,
        "enterpriseId": account.get("enterpriseId") or "",
        "departmentFullName": account.get("departmentFullName") or "",
        "expiresAt": auth.get("expiresAt") or 0,
        "refreshExpiresAt": auth.get("refreshExpiresAt") or 0,
    }


# ── 请求头 ────────────────────────────────────────────────


def build_headers(session: dict, stream: bool = False) -> dict:
    """组装上游网关需要的请求头（对齐 buildAuthHeaders + IDE 标识）。"""
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
        "User-Agent": API_UA,
        "X-IDE-Type": "WorkBuddy",
        "X-IDE-Name": "WorkBuddy",
        "X-IDE-Version": CLIENT_VERSION,
        "X-Product": "WorkBuddy",
        "Authorization": f"Bearer {session.get('accessToken', '')}",
    }
    if session.get("uid"):
        headers["X-User-Id"] = session["uid"]
    if session.get("domain"):
        headers["X-Domain"] = session["domain"]
    if session.get("enterpriseId"):
        headers["X-Enterprise-Id"] = session["enterpriseId"]
        headers["X-Tenant-Id"] = session["enterpriseId"]
    if session.get("departmentFullName"):
        headers["X-Department-Info"] = session["departmentFullName"]
    return headers


def _is_expired(session: dict) -> bool:
    exp = session.get("expiresAt") or 0
    return bool(exp) and (exp - EXPIRE_SKEW_MS) < int(time.time() * 1000)


async def refresh_session(session: dict, client: httpx.AsyncClient) -> Optional[dict]:
    """用 refreshToken 续期，返回更新后的 session。"""
    refresh = session.get("refreshToken")
    if not refresh:
        return None
    try:
        res = await client.post(
            f"{API_BASE}/v2/plugin/auth/token/refresh",
            headers={
                **build_headers(session),
                "X-Refresh-Token": refresh,
                "X-Auth-Refresh-Source": "plugin",
            },
            json={},
        )
    except Exception:
        return None
    if res.status_code >= 400:
        return None
    try:
        data = (res.json() or {}).get("data") or {}
    except Exception:
        return None
    token = data.get("accessToken")
    if not token:
        return None

    out = dict(session)
    out["accessToken"] = token
    if data.get("refreshToken"):
        out["refreshToken"] = data["refreshToken"]
    now = int(time.time() * 1000)
    if data.get("expiresIn"):
        out["expiresAt"] = now + int(data["expiresIn"]) * 1000
    else:
        out["expiresAt"] = now + 60 * 60 * 1000
    if data.get("refreshExpiresIn"):
        out["refreshExpiresAt"] = now + int(data["refreshExpiresIn"]) * 1000
    return out


def _cache_key(session: dict) -> str:
    raw = session.get("accessToken") or session.get("refreshToken") or ""
    return hashlib.sha256(raw.encode()).hexdigest()


# ── 对外主入口 ────────────────────────────────────────────


def session_from_credentials(credentials: dict) -> Optional[dict]:
    """把账号配置（加密落盘字段）还原成上游会话。

    credentials 优先级：
      1. wb_access_token（导入时落库的 per-account token）
      2. 本机 Desktop 凭证文件
    """
    if credentials.get("wb_access_token"):
        return {
            "accessToken": credentials["wb_access_token"],
            "refreshToken": credentials.get("wb_refresh_token") or "",
            "uid": credentials.get("wb_uid") or "",
            "nickname": "",
            "domain": DEFAULT_DOMAIN,
            "enterpriseId": "",
            "departmentFullName": "",
            "expiresAt": 0,
            "refreshExpiresAt": 0,
        }
    return read_local_session()


async def get_session(
    credentials: dict, client: Optional[httpx.AsyncClient] = None
) -> Optional[dict]:
    """获取（并缓存）可用的上游会话，必要时自动续期。"""
    session = session_from_credentials(credentials)
    if not session:
        return None

    key = _cache_key(session)
    with _cache_lock:
        hit = _cache.get(key)
    if hit and not _is_expired(hit["session"]):
        return hit["session"]

    owns = client is None
    if owns:
        client = httpx.AsyncClient(timeout=30.0)
    try:
        if _is_expired(session):
            refreshed = await refresh_session(session, client)
            if refreshed:
                session = refreshed
                key = _cache_key(session)
        with _cache_lock:
            _cache[key] = {"session": session, "at": time.time()}
        return session
    except Exception:
        return session
    finally:
        if owns:
            await client.aclose()


def invalidate_session_cache(access_token: Optional[str] = None) -> None:
    with _cache_lock:
        if access_token is None:
            _cache.clear()
            return
        _cache.pop(hashlib.sha256(access_token.encode()).hexdigest(), None)


async def get_account_quota(credentials: dict) -> dict:
    """云端代理配额。成功返回原始 data，失败返回 {error}。"""
    async with httpx.AsyncClient(timeout=15.0) as client:
        session = await get_session(credentials, client)
        if not session:
            return {"error": "no-session"}
        try:
            res = await client.get(
                f"{API_BASE}/v2/user/cloudagent/quota",
                headers=build_headers(session),
            )
            if res.status_code >= 400:
                return {"error": f"http-{res.status_code}"}
            data = res.json() or {}
            if data.get("code") not in (0, None):
                return {"error": data.get("msg") or "bad-response"}
            return data.get("data") or {}
        except Exception as e:
            return {"error": str(e)}


async def list_remote_models(credentials: dict) -> list[str]:
    """拉取当前账号可用模型（失败返回空列表）。"""
    async with httpx.AsyncClient(timeout=20.0) as client:
        session = await get_session(credentials, client)
        if not session:
            return []
        for url in (
            f"{API_BASE}/v2/enterprises/personal/models",
            f"{API_BASE}/v3/config",
        ):
            try:
                res = await client.get(url, headers=build_headers(session))
                if res.status_code >= 400:
                    continue
                data = res.json() or {}
                if data.get("code") not in (0, None):
                    continue
                models = _collect_models(data.get("data"))
                if models:
                    return models
            except Exception:
                continue
    return []


def _collect_models(node: Any) -> list[str]:
    """从 /v3/config 或 /v2/enterprises/personal/models 响应里收集模型 ID。"""
    out: list[str] = []

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            models = n.get("models")
            if isinstance(models, list):
                for m in models:
                    if isinstance(m, str) and m:
                        out.append(m)
                    elif isinstance(m, dict) and m.get("id"):
                        out.append(m["id"])
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    seen: set[str] = set()
    uniq = []
    for m in out:
        if m not in seen:
            seen.add(m)
            uniq.append(m)
    return uniq

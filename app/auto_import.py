"""WorkBuddy Desktop 凭证自动导入

只读本机 WorkBuddy Desktop 共享凭证文件：
  Windows: %LOCALAPPDATA%/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
  macOS  : ~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
  Linux  : ~/.local/share/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
"""

from __future__ import annotations

from typing import Any

from .workbuddy_session import auth_file_path, read_local_session


async def auto_import_desktop() -> dict[str, Any]:
    sess = read_local_session()
    if not sess:
        path = auth_file_path()
        where = str(path) if path else "credential file not found"
        return {
            "found": False,
            "error": (
                "WorkBuddy Desktop session not found.\n"
                f"Credential file: {where}\n"
                "Sign in to WorkBuddy Desktop once, then retry."
            ),
        }

    return {
        "found": True,
        "source": "desktop-credential-file",
        "wbAccessToken": sess["accessToken"],
        "wbRefreshToken": sess.get("refreshToken"),
        "wbUid": sess.get("uid"),
        "uid": sess.get("uid"),
        "nickname": sess.get("nickname"),
    }


def apply_import_payload(data: dict) -> dict:
    return {
        "uid": data.get("uid") or data.get("wbUid") or "",
        "wb_access_token": data.get("wbAccessToken") or "",
        "wb_uid": data.get("wbUid") or "",
        "wb_refresh_token": data.get("wbRefreshToken") or "",
    }

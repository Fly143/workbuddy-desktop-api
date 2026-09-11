"""WorkBuddy Desktop API — 主入口

将WorkBuddy Desktop 账号会话转换为 OpenAI + Anthropic 兼容 API。
"""

import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.routes import router, _do_discover
from app.config import config_manager
from app.anthropic_routes import router as anthropic_router
from app.batch import init_batch_storage as init_anthropic_batches

app = FastAPI(
    title="WorkBuddy Desktop API",
    description="WorkBuddy Desktop session → OpenAI + Anthropic API (Chat / Responses / Anthropic Messages)",
    version="1.1.9",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup_discover_models():
    init_anthropic_batches(str(Path(__file__).parent / ".anthropic_batches"))
    await _auto_import_local_session()
    try:
        await _do_discover()
        print("模型预探测完成")
    except Exception as e:
        print(f"模型预探测失败（不影响服务）: {e}")


async def _auto_import_local_session():
    """首次启动且未配置账号时，自动导入本机 WorkBuddy Desktop 会话。

    凭证文件：%LOCALAPPDATA%/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
    """
    if config_manager.config.workbuddy_accounts:
        return
    try:
        from app.auto_import import auto_import_desktop, apply_import_payload
        from app.routes import _validate_and_save

        payload = await auto_import_desktop()
        if not payload.get("found"):
            print(f"[启动] 未在本机发现 WorkBuddy Desktop 会话：{payload.get('error', '')}")
            return
        fields = apply_import_payload(payload)
        result = await _validate_and_save(
            fields["wb_access_token"],
            fields["wb_uid"],
            fields["wb_refresh_token"],
            fields.get("uid") or "",
        )
        print(f"[启动] 已自动导入本机 WorkBuddy Desktop 会话：{result}")
    except Exception as e:
        print(f"[启动] 自动导入失败（可在管理页手动导入）: {e}")



app.include_router(router)
app.include_router(anthropic_router)

init_anthropic_batches(str(Path(__file__).parent / ".anthropic_batches"))

web_dir = Path(__file__).parent / "web"
if web_dir.exists():
    app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")


def main():
    port = int(os.getenv("PORT", "8080"))
    host = os.getenv("HOST", "0.0.0.0")

    print(f"""
WorkBuddy Desktop API
  地址: http://{host}:{port}
  管理: http://{host}:{port}
  API:  http://{host}:{port}/v1/chat/completions
  文档: http://{host}:{port}/docs

  API Keys: {len(config_manager.config.api_keys.split(','))} 个
  Desktop 账号: {len(config_manager.config.workbuddy_accounts)} 个
  模型: hy4-preview / hy3
""")

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()

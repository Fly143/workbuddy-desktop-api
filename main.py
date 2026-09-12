"""WorkBuddy Desktop API 鈥?涓诲叆鍙?

灏哤orkBuddy Desktop 璐﹀彿浼氳瘽杞崲涓?OpenAI + Anthropic 鍏煎 API銆?
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
    description="WorkBuddy Desktop session 鈫?OpenAI + Anthropic API (Chat / Responses / Anthropic Messages)",
    version="1.2.2",
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
        print("妯″瀷棰勬帰娴嬪畬鎴?)
    except Exception as e:
        print(f"妯″瀷棰勬帰娴嬪け璐ワ紙涓嶅奖鍝嶆湇鍔★級: {e}")


async def _auto_import_local_session():
    """棣栨鍚姩涓旀湭閰嶇疆璐﹀彿鏃讹紝鑷姩瀵煎叆鏈満 WorkBuddy Desktop 浼氳瘽銆?

    鍑瘉鏂囦欢锛?LOCALAPPDATA%/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
    """
    if config_manager.config.workbuddy_accounts:
        return
    try:
        from app.auto_import import auto_import_desktop, apply_import_payload
        from app.routes import _validate_and_save

        payload = await auto_import_desktop()
        if not payload.get("found"):
            print(f"[鍚姩] 鏈湪鏈満鍙戠幇 WorkBuddy Desktop 浼氳瘽锛歿payload.get('error', '')}")
            return
        fields = apply_import_payload(payload)
        result = await _validate_and_save(
            fields["wb_access_token"],
            fields["wb_uid"],
            fields["wb_refresh_token"],
            fields.get("uid") or "",
        )
        print(f"[鍚姩] 宸茶嚜鍔ㄥ鍏ユ湰鏈?WorkBuddy Desktop 浼氳瘽锛歿result}")
    except Exception as e:
        print(f"[鍚姩] 鑷姩瀵煎叆澶辫触锛堝彲鍦ㄧ鐞嗛〉鎵嬪姩瀵煎叆锛? {e}")



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
  鍦板潃: http://{host}:{port}
  绠＄悊: http://{host}:{port}
  API:  http://{host}:{port}/v1/chat/completions
  鏂囨。: http://{host}:{port}/docs

  API Keys: {len(config_manager.config.api_keys.split(','))} 涓?
  Desktop 璐﹀彿: {len(config_manager.config.workbuddy_accounts)} 涓?
  妯″瀷: hy4-preview / hy3
""")

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()

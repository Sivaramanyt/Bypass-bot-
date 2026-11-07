from __future__ import annotations

import os
import re
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.resolver import ResolveError, resolve_url
from app.storage import save_log


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
SUPABASE_TABLE = os.getenv("SUPABASE_TABLE", "resolver_logs").strip()
MAX_REDIRECTS = max(1, min(int(os.getenv("MAX_REDIRECTS", "10")), 20))
REQUEST_TIMEOUT = max(3.0, min(float(os.getenv("REQUEST_TIMEOUT", "12")), 30.0))
MAX_REQUESTS_PER_MINUTE = max(1, min(int(os.getenv("MAX_REQUESTS_PER_MINUTE", "20")), 60))

if WEBHOOK_SECRET and not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", WEBHOOK_SECRET):
    raise RuntimeError("WEBHOOK_SECRET must contain only letters, numbers, underscore or hyphen.")

_user_hits: dict[int, deque[float]] = defaultdict(deque)
URL_PATTERN = re.compile(r"https?://[^\s<>]+|(?:www\.)?[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}(?:/[^\s<>]*)?", re.I)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    _user_hits.clear()


app = FastAPI(title="Short Link Resolver Bot", version="1.0.0", lifespan=lifespan)


def _limited(user_id: int) -> bool:
    now = time.monotonic()
    hits = _user_hits[user_id]
    while hits and now - hits[0] > 60:
        hits.popleft()
    if len(hits) >= MAX_REQUESTS_PER_MINUTE:
        return True
    hits.append(now)
    return False


def _extract_url(message: str) -> str | None:
    match = URL_PATTERN.search(message or "")
    if not match:
        return None
    return match.group(0).rstrip(".,!?)\\]}>")


async def _telegram(method: str, payload: dict) -> dict:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not configured")
    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
        response = await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=payload)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError("Telegram API rejected the request")
        return data


async def _reply(chat_id: int, text: str) -> None:
    await _telegram("sendMessage", {"chat_id": chat_id, "text": text, "disable_web_page_preview": True})


@app.get("/")
async def root():
    return {"service": "Short Link Resolver Bot", "version": "1.0.0", "status": "ok"}


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.get("/setup-webhook/{secret}")
async def setup_webhook(secret: str):
    if not WEBHOOK_SECRET or secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=404, detail="Not found")
    if not BOT_TOKEN or not PUBLIC_BASE_URL:
        raise HTTPException(status_code=503, detail="BOT_TOKEN and PUBLIC_BASE_URL must be configured")
    parsed = urlsplit(PUBLIC_BASE_URL)
    if parsed.scheme != "https" or not parsed.netloc:
        raise HTTPException(status_code=500, detail="PUBLIC_BASE_URL must be an HTTPS URL")
    webhook_url = f"{PUBLIC_BASE_URL}/telegram/{WEBHOOK_SECRET}"
    try:
        result = await _telegram("setWebhook", {
            "url": webhook_url,
            "secret_token": WEBHOOK_SECRET,
            "allowed_updates": ["message"],
            "drop_pending_updates": False,
        })
        return {"ok": True, "webhook": "registered", "telegram": result.get("description", "Webhook set")}
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Telegram webhook registration failed") from exc


@app.post("/telegram/{secret}")
async def telegram_webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    if not WEBHOOK_SECRET or secret != WEBHOOK_SECRET or x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        update = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id = chat.get("id")
    user_id = sender.get("id")
    text = message.get("text") or message.get("caption") or ""
    if not chat_id:
        return JSONResponse({"ok": True})

    if text.startswith("/start") or text.startswith("/help"):
        await _reply(chat_id,
            "Hi! Send me an ordinary HTTP/HTTPS short link and I will follow its normal redirects.\n\n"
            "I do not bypass CAPTCHA, login pages, mandatory ad timers, paywalls, or JavaScript challenges.\n"
            "Only send links you trust.")
        return {"ok": True}

    input_url = _extract_url(text)
    if not input_url:
        await _reply(chat_id, "Please send a valid HTTP/HTTPS link.")
        return {"ok": True}

    if user_id is not None and _limited(int(user_id)):
        await _reply(chat_id, "Rate limit reached. Please wait a minute and try again.")
        return {"ok": True}

    try:
        final_url, hops = await resolve_url(input_url, MAX_REDIRECTS, REQUEST_TIMEOUT)
        await save_log(
            supabase_url=SUPABASE_URL, supabase_key=SUPABASE_KEY, table=SUPABASE_TABLE,
            user_id=user_id, input_url=input_url, final_url=final_url, status="success",
        )
        await _reply(chat_id, f"Final link:\n{final_url}\n\nRedirects followed: {hops}")
    except ResolveError as exc:
        await save_log(
            supabase_url=SUPABASE_URL, supabase_key=SUPABASE_KEY, table=SUPABASE_TABLE,
            user_id=user_id, input_url=input_url, final_url=None, status="failed", error=str(exc),
        )
        await _reply(chat_id, f"Could not resolve this link:\n{exc}")
    except Exception:
        await _reply(chat_id, "Something went wrong while processing that link. Please try again later.")
    return {"ok": True}

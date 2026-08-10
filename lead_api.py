import logging
import os
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_GROUP_CHAT_ID = os.getenv("ADMIN_GROUP_CHAT_ID", "").strip()

_DEFAULT_ORIGINS = (
    "http://127.0.0.1:5500,"
    "http://localhost:5500,"
    "http://127.0.0.1:8000,"
    "http://localhost:8000,"
    "https://satmakon.github.io"
)
_raw_origins = os.getenv("LEAD_ALLOWED_ORIGINS", _DEFAULT_ORIGINS)
ALLOWED_ORIGINS = [origin.strip() for origin in _raw_origins.split(",") if origin.strip()]

app = FastAPI(title="SATMakon Lead API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)

# Simple in-memory anti-spam protection: 5 requests per IP per 10 minutes.
_REQUEST_WINDOW_SECONDS = 10 * 60
_MAX_REQUESTS_PER_WINDOW = 5
_request_log: dict[str, deque[float]] = defaultdict(deque)
TASHKENT_TZ = timezone(timedelta(hours=5), name="Asia/Tashkent")


class LeadRequest(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    phone: str = Field(min_length=7, max_length=40)
    source: str = Field(default="website", max_length=200)
    website: str = Field(default="", max_length=200)  # Honeypot field for bots.

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = " ".join(value.strip().split())
        if len(value) < 2:
            raise ValueError("Name is too short")
        return value

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        value = value.strip()
        digits = re.sub(r"\D", "", value)
        if not 7 <= len(digits) <= 15:
            raise ValueError("Invalid phone number")
        return value


def _check_rate_limit(client_ip: str) -> None:
    """Reject excessive successful submissions without counting failed attempts."""
    if client_ip in {"127.0.0.1", "::1", "localhost"}:
        return

    now = time.monotonic()
    bucket = _request_log[client_ip]
    while bucket and now - bucket[0] > _REQUEST_WINDOW_SECONDS:
        bucket.popleft()
    if len(bucket) >= _MAX_REQUESTS_PER_WINDOW:
        raise HTTPException(status_code=429, detail="Too many requests. Try again later.")


def _record_successful_request(client_ip: str) -> None:
    if client_ip not in {"127.0.0.1", "::1", "localhost"}:
        _request_log[client_ip].append(time.monotonic())


def _format_lead_message(lead: LeadRequest) -> str:
    timestamp = datetime.now(TASHKENT_TZ).strftime("%Y-%m-%d %H:%M")
    return (
        "🆕 Yangi konsultatsiya so'rovi\n\n"
        f"👤 Ism: {lead.name}\n"
        f"📞 Telefon: {lead.phone}\n"
        f"🌐 Manba: {lead.source}\n"
        f"🕒 Vaqt: {timestamp}"
    )


async def _send_to_telegram(text: str) -> None:
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not configured")
        raise HTTPException(status_code=503, detail="Telegram bot is not configured")

    try:
        chat_id = int(ADMIN_GROUP_CHAT_ID)
    except (TypeError, ValueError):
        logger.error("ADMIN_GROUP_CHAT_ID is missing or invalid")
        raise HTTPException(status_code=503, detail="Admin group is not configured")

    # Do not log the complete URL: it contains the bot token.
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, json=payload)
    except httpx.HTTPError as exc:
        logger.exception("Telegram request failed")
        raise HTTPException(status_code=502, detail="Telegram is temporarily unavailable") from exc

    try:
        data = response.json()
    except ValueError:
        data = {}

    if response.status_code != 200 or not data.get("ok"):
        description = data.get("description") or f"HTTP {response.status_code}"
        logger.error("Telegram rejected lead notification: %s", description)
        raise HTTPException(status_code=502, detail=f"Telegram error: {description}")


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "ok": True,
        "telegram_configured": bool(TELEGRAM_TOKEN and ADMIN_GROUP_CHAT_ID),
    }


@app.post("/api/leads")
async def submit_lead(lead: LeadRequest, request: Request) -> dict[str, bool]:
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip)

    # Real visitors never fill this hidden field. Silently accept spam bots.
    if lead.website:
        return {"ok": True}

    try:
        message = _format_lead_message(lead)
        await _send_to_telegram(message)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Unexpected lead submission error")
        raise HTTPException(status_code=500, detail="Internal lead processing error") from exc

    _record_successful_request(client_ip)
    logger.info("Website lead sent to admin group from %s", client_ip)
    return {"ok": True}


# When the website is copied into ./public, the same process serves both the
# frontend and /api/leads. Mount this last so API routes keep priority.
PUBLIC_DIR = BASE_DIR / "public"
if PUBLIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=PUBLIC_DIR, html=True), name="website")

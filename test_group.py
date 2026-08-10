import asyncio
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


async def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("ADMIN_GROUP_CHAT_ID", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing in .env")
    if not chat_id:
        raise SystemExit("ADMIN_GROUP_CHAT_ID is missing in .env")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            url,
            json={"chat_id": int(chat_id), "text": "✅ SATMakon test: group connection works."},
        )
    data = response.json()
    if response.status_code != 200 or not data.get("ok"):
        raise SystemExit(f"Telegram error: {data.get('description', response.text)}")
    print("Test message sent successfully.")


if __name__ == "__main__":
    asyncio.run(main())

import os
import re
import json
import time
import asyncio
import logging
import threading
from pathlib import Path
from datetime import date, timedelta
from openai import OpenAI
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from prompts import SYSTEM_PROMPT
from sheets import log_message, log_visit

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
# httpx logs Telegram request URLs, which contain the secret bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
ADMIN_GROUP_CHAT_ID = os.getenv("ADMIN_GROUP_CHAT_ID", "").strip()

# Keep runtime state in a predictable directory next to bot.py.
# This avoids creating JSON files in whichever folder the process was started from.
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

MEMORY_FILE = DATA_DIR / "memory.json"
PHONES_FILE = DATA_DIR / "phones.json"
NOTIFIED_FILE = DATA_DIR / "notified.json"


def _is_empty_json_file(path: Path) -> bool:
    """Return True when a JSON file is missing, blank, or contains an empty object."""
    if not path.exists():
        return True
    try:
        content = path.read_text(encoding="utf-8").strip()
        return not content or json.loads(content) == {}
    except (OSError, json.JSONDecodeError):
        return False


def _migrate_legacy_json_files() -> None:
    """Move old root-level JSON state files into data/ without overwriting real data."""
    for filename, target in (
        ("memory.json", MEMORY_FILE),
        ("phones.json", PHONES_FILE),
        ("notified.json", NOTIFIED_FILE),
    ):
        legacy = BASE_DIR / filename
        if legacy == target or not legacy.exists():
            continue

        if _is_empty_json_file(target):
            try:
                legacy.replace(target)
                logger.info(f"Migrated {filename} to {target.relative_to(BASE_DIR)}")
            except OSError as exc:
                logger.warning(f"Could not migrate {filename}: {exc}")
        else:
            logger.warning(
                f"Skipped legacy {filename}: {target.relative_to(BASE_DIR)} already contains data"
            )


def _ensure_data_files() -> None:
    """Create valid empty JSON files on a fresh installation."""
    for path in (MEMORY_FILE, PHONES_FILE, NOTIFIED_FILE):
        if not path.exists():
            try:
                path.write_text("{}\n", encoding="utf-8")
            except OSError as exc:
                logger.warning(f"Could not create {path.name}: {exc}")


_migrate_legacy_json_files()
_ensure_data_files()

client = (
    OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com/v1")
    if DEEPSEEK_API_KEY
    else None
)

# Per-user conversation memory: {user_id: [messages]}
def load_histories() -> dict:
    try:
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {int(k): v for k, v in data.items()}
    except Exception as e:
        logger.warning(f"Memory load failed: {e}")
    return {}

def save_histories() -> None:
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in histories.items()}, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Memory save failed: {e}")

histories: dict[int, list] = load_histories()
logger.info(f"Loaded memory for {len(histories)} users.")


def load_phones() -> dict:
    try:
        if os.path.exists(PHONES_FILE):
            with open(PHONES_FILE, "r", encoding="utf-8") as f:
                return {int(k): v for k, v in json.load(f).items()}
    except Exception as e:
        logger.warning(f"Phones load failed: {e}")
    return {}


def save_phones() -> None:
    try:
        with open(PHONES_FILE, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in user_phones.items()}, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Phones save failed: {e}")


user_phones: dict[int, str] = load_phones()


def load_notified() -> dict:
    try:
        if os.path.exists(NOTIFIED_FILE):
            with open(NOTIFIED_FILE, "r", encoding="utf-8") as f:
                return {int(k): v for k, v in json.load(f).items()}
    except Exception as e:
        logger.warning(f"Notified load failed: {e}")
    return {}


def save_notified() -> None:
    try:
        with open(NOTIFIED_FILE, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in notified_visits.items()}, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Notified save failed: {e}")


# Dedupe admin-group notifications by user_id → "date|time|topic" hash
notified_visits: dict[int, str] = load_notified()

# Users we're waiting a phone number from
waiting_for_phone: set[int] = set()

# Debounce: track last message time and buffer per user
import time as _time
pending_messages: dict[int, list] = {}
last_message_time: dict[int, float] = {}
pending_tasks: dict[int, asyncio.Task] = {}
DEBOUNCE_SECONDS = 3.2

def is_phone_number(text: str) -> bool:
    digits = re.sub(r"[\s\-\(\)\+]", "", text)
    return digits.isdigit() and len(digits) >= 7


def clean_reply(text: str) -> str:
    # Strip markdown bold/italic that Telegram won't render
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'_(.+?)_', r'\1', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    # Remove markdown headers
    text = re.sub(r'^#{1,3}\s+', '', text, flags=re.MULTILINE)
    # Replace banned ending questions with correct ones
    replacements = [
        (r'[Qq]aysi AP fan[a-z]* qiziq[a-z]*\?', 'Qaysi birini tanlaysiz?'),
        (r'[Qq]aysi fan[a-z]* qiziq[a-z]*\?', 'Qaysi birini tanlaysiz?'),
        (r'[Qq]aysi kurs[a-z]* qiziq[a-z]*\?', 'Qaysi birini tanlaysiz?'),
    ]
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text)
    # Remove makon.ai references
    text = re.sub(r'[Mm]akon\.ai\s*(platformasi|orqali|da|bilan)?\s*', '', text).strip()
    # Fix common grammar mistakes
    text = re.sub(r'\bSizni\b', 'Sizga', text)
    text = re.sub(r'\bsizni\b', 'sizga', text)
    # Move leading emojis to end of their line
    emoji_pattern = re.compile(r'^([\U00010000-\U0010ffff\U00002600-\U000027BF\U0001F300-\U0001FAFF☀-➿]+)\s*(.+)', flags=re.MULTILINE)
    text = emoji_pattern.sub(r'\2 \1', text)
    # Clean up stray separators if any remain after splitting
    text = text.replace("||", " ").strip()
    return text


REVIEW_PROMPT = """Quyidagi Telegram xabarini tekshir va to'g'irla. Faqat to'g'irlangan xabarni qaytarish kerak — hech qanday izoh, tushuntirish yoki qo'shimcha matn yozma.

Tekshirish kerak bo'lgan narsalar:
1. O'zbek grammatikasi — "Sizni" o'rniga "Sizga", noto'g'ri kelishiklar
2. Ruscha yozilgan bo'lsa — ruscha grammatika
3. Inglizcha yozilgan bo'lsa — inglizcha grammatika
4. Fikr mantiqiy va tushunarli bo'lsin
5. Juda uzun bo'lsa — qisqartir
6. ** yoki _ yoki ` belgilari bo'lsa — o'chir
7. || belgisi bo'lsa — saqlash kerak (ular alohida xabar uchun)

Faqat to'g'irlangan xabarni qaytarish — boshqa hech narsa yozma."""


def review_reply(reply: str) -> str:
    if client is None:
        return reply
    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": REVIEW_PROMPT},
                {"role": "user", "content": reply},
            ],
            temperature=0.1,
            max_tokens=400,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.warning(f"Review step failed: {e}")
        return reply


COMMITMENT_KEYWORDS = [
    "kelaman", "boraman", "kelamiz", "boramiz", "kelib", "tashrif",
    "приду", "прийду", "приеду", "буду", "зайду", "придём", "придем",
    "coming", "i'll come", "i will come", "will be there", "stop by",
]

WEEKDAYS_UZ = ["dushanba", "seshanba", "chorshanba", "payshanba", "juma", "shanba", "yakshanba"]


def has_commitment(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in COMMITMENT_KEYWORDS)


def extract_booking(history: list) -> dict | None:
    """Ask DeepSeek to extract visit date/time/topic from recent conversation."""
    if not history or client is None:
        return None

    today = date.today()
    tomorrow = today + timedelta(days=1)
    day_after = today + timedelta(days=2)
    weekday_uz = WEEKDAYS_UZ[today.weekday()]

    system_prompt = f"""You are an information extractor. Read the SATMakon Telegram conversation and return ONLY a JSON object.

Today: {today.isoformat()} ({weekday_uz})
Tomorrow (ertaga): {tomorrow.isoformat()}
Day after tomorrow (indinga/indin): {day_after.isoformat()}
Working hours: 09:00–20:00.

The user speaks Uzbek, Russian or English. Detect if the user committed to visit the center. Extract:

{{
  "has_visit": true or false,
  "visit_date": "YYYY-MM-DD" or null,   // null if user said they will come but didn't specify a date
  "visit_time": "HH:MM" or null,        // null if no time given
  "topic": "short 2-4 word summary in Uzbek, e.g. 'SAT kurslari', 'AP Calculus narxi', 'ustozlar haqida'"
}}

Uzbek date hints: "bugun"=today, "ertaga"=tomorrow, "indinga"/"indinkuni"=day after tomorrow.
Time hints: "soat 10 da"=10:00, "soat 3"=15:00 (assume afternoon if ambiguous in working hours).
If the user only said something like "kelaman" with no date/time, set has_visit=true, visit_date=null, visit_time=null.
Return ONLY the JSON, no other text."""

    recent = history[-12:]
    convo = "\n".join(f"{m['role']}: {m['content']}" for m in recent)

    try:
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": convo},
            ],
            temperature=0.1,
            max_tokens=200,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content.strip())
        if data.get("has_visit"):
            return data
    except Exception as e:
        logger.warning(f"Booking extraction failed: {e}")
    return None


def get_reply(user_id: int, user_message: str) -> str:
    if client is None:
        raise RuntimeError("DeepSeek is not configured")

    history = histories.get(user_id, [])
    history.append({"role": "user", "content": user_message})

    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "system", "content": SYSTEM_PROMPT}] + history[-20:],
                temperature=0.85,
                max_tokens=600,
            )
            reply = clean_reply(response.choices[0].message.content.strip())
            reply = review_reply(reply)
            reply = clean_reply(reply)
            history.append({"role": "assistant", "content": reply})
            histories[user_id] = history
            save_histories()
            return reply
        except Exception as e:
            logger.warning(f"DeepSeek attempt {attempt + 1} failed: {e}")
            error_text = str(e).lower()
            if "402" in error_text or "insufficient balance" in error_text:
                histories[user_id] = history
                save_histories()
                raise
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                histories.pop(user_id, None)
                raise


async def start(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    histories.pop(user_id, None)
    waiting_for_phone.discard(user_id)
    notified_visits.pop(user_id, None)
    save_histories()
    save_notified()
    await update.message.reply_text("Assalomu Alaykum. SATMakon o'quv markazidan Ziyodaman. Labbay?")


def _format_admin_message(tg_user, phone: str, booking: dict) -> str:
    name = tg_user.full_name or "Mijoz"
    uname = f"@{tg_user.username}" if tg_user.username else ""
    header = f"{name} {uname}".strip()
    topic = booking.get("topic") or "aniqlanmagan"
    visit_date = booking.get("visit_date") or "aniqlanmagan"
    visit_time = booking.get("visit_time") or ""
    when = visit_date + (f" {visit_time}" if visit_time else "")
    phone_line = phone if phone else "berilmagan"
    request_text = (booking.get("request") or "").strip()
    request_line = f"\n💬 So'rov: {request_text}" if request_text else ""

    return (
        "🆕 Yangi mijoz tashrifga keladi\n"
        "\n"
        f"👤 {header}\n"
        f"📞 {phone_line}\n"
        f"🎯 Mavzu: {topic}\n"
        f"📅 Sana: {when}"
        f"{request_line}"
    )


async def notify_admin_group(context, tg_user, phone: str, booking: dict) -> None:
    if not ADMIN_GROUP_CHAT_ID:
        return
    try:
        chat_id = int(ADMIN_GROUP_CHAT_ID)
    except ValueError:
        logger.warning(f"Invalid ADMIN_GROUP_CHAT_ID: {ADMIN_GROUP_CHAT_ID}")
        return

    key = f"{phone}|{booking.get('visit_date') or ''}|{booking.get('visit_time') or ''}|{booking.get('topic') or ''}"
    if notified_visits.get(tg_user.id) == key:
        logger.info(f"Skipping admin notify for {tg_user.id} — same booking already notified ({key!r})")
        return

    text = _format_admin_message(tg_user, phone, booking)
    try:
        await context.bot.send_message(chat_id=chat_id, text=text)
        notified_visits[tg_user.id] = key
        save_notified()
        logger.info(f"Admin group notified for user {tg_user.id} (key={key!r})")
    except Exception as e:
        logger.warning(f"Admin group notify failed: {e}")


async def try_log_visit(tg_user, user_id: int, context, *, notify: bool = False) -> None:
    """Log to Taqvim and (when notify=True) ping admin group.

    When notify=True (phone collected), we always notify even if the LLM
    extractor can't pin down a visit phrase — the phone itself is the strong
    commitment signal. The booking dict just enriches the message.
    """
    history = histories.get(user_id, [])
    booking = extract_booking(history) or {}
    latest_request = next(
        (item.get("content", "") for item in reversed(history) if item.get("role") == "user"),
        "",
    )
    if latest_request:
        booking.setdefault("request", latest_request[:500])
        booking.setdefault("topic", "Telegram murojaati")

    if not notify and not booking:
        return

    phone = user_phones.get(user_id, "")
    if booking:
        try:
            log_visit(
                username=tg_user.username,
                full_name=tg_user.full_name,
                user_id=tg_user.id,
                phone=phone,
                visit_date=booking.get("visit_date") or "",
                visit_time=booking.get("visit_time") or "",
                topic=booking.get("topic") or "",
            )
        except Exception as e:
            logger.warning(f"log_visit error: {e}")

    if notify:
        await notify_admin_group(context, tg_user, phone, booking)


async def process_user_message(user_id: int, combined_text: str, update: Update, context) -> None:
    """Called after debounce delay with combined text."""

    # Phone number state check
    if user_id in waiting_for_phone:
        if is_phone_number(combined_text):
            waiting_for_phone.discard(user_id)
            user_phones[user_id] = combined_text.strip()
            save_phones()
            await update.message.reply_text("Rahmat, kutib qolamiz! 😊")
            await try_log_visit(update.effective_user, user_id, context, notify=True)
            return
        else:
            await update.message.reply_text("Raqamingizni yuboring 😊")
            return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        reply = get_reply(user_id, combined_text)
    except Exception as e:
        logger.error(f"DeepSeek unavailable: {e}")
        history = histories.get(user_id, [])
        if not history or history[-1].get("content") != combined_text:
            history.append({"role": "user", "content": combined_text})
        histories[user_id] = history
        save_histories()
        waiting_for_phone.add(user_id)
        await update.message.reply_text(
            "AI konsultant vaqtincha ishlamayapti. So'rovingizni administratorga "
            "yuborish uchun telefon raqamingizni yozing 😊"
        )
        return

    # If bot is asking for phone number, set state
    if any(kw in reply.lower() for kw in ["telefon raqam", "raqamingiz", "phone number", "номер телефона"]):
        waiting_for_phone.add(user_id)

    logger.info(f"Bot → {user_id}: {reply[:80]}...")
    parts = [p.strip() for p in reply.split("||") if p.strip()]
    if len(parts) > 1:
        for part in parts:
            await update.message.reply_text(part)
            await asyncio.sleep(1.4)
    else:
        await update.message.reply_text(reply)

    tg_user = update.effective_user
    try:
        log_message(
            username=tg_user.username,
            full_name=tg_user.full_name,
            user_id=tg_user.id,
            user_msg=combined_text,
            bot_reply=reply,
        )
    except Exception as e:
        logger.warning(f"Sheets error: {e}")

    if has_commitment(combined_text):
        await try_log_visit(tg_user, user_id, context, notify=False)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_text = update.message.text

    if not user_text:
        return

    logger.info(f"User {user_id}: {user_text}")

    # Buffer message and record time
    if user_id not in pending_messages:
        pending_messages[user_id] = []
    pending_messages[user_id].append(user_text)
    last_message_time[user_id] = _time.monotonic()

    # If a watcher task is already running for this user, it will pick up the new message
    if user_id in pending_tasks and not pending_tasks[user_id].done():
        return

    # Start a watcher task
    async def watcher():
        while True:
            await asyncio.sleep(0.3)
            elapsed = _time.monotonic() - last_message_time.get(user_id, 0)
            if elapsed >= DEBOUNCE_SECONDS:
                break
        messages = pending_messages.pop(user_id, [])
        pending_tasks.pop(user_id, None)
        last_message_time.pop(user_id, None)
        if messages:
            combined = " ".join(messages)
            await process_user_message(user_id, combined, update, context)

    pending_tasks[user_id] = asyncio.create_task(watcher())


async def reset(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    histories.pop(user_id, None)
    save_histories()
    try:
        greeting = get_reply(
            user_id,
            "Yangi o'quvchi yoki ota-ona Telegram orqali murojaat qildi. Ularni Nilufar sifatida salom bilan kutib ol.",
        )
        await update.message.reply_text(greeting)
    except Exception as e:
        logger.error(f"Reset error: {e}")


async def chatid(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    title = chat.title or chat.full_name or "(private)"
    await update.message.reply_text(f"chat_id: {chat.id}\ntype: {chat.type}\ntitle: {title}")


async def admin_stats(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    total_users = len(histories)
    await update.message.reply_text(
        f"📊 Joriy sessiya statistikasi:\n"
        f"Faol foydalanuvchilar: {total_users} ta\n"
        f"Google Sheets'da to'liq tarix mavjud."
    )


def start_lead_api() -> None:
    if os.getenv("ENABLE_LEAD_API", "1").strip().lower() in {"0", "false", "no"}:
        logger.info("Website lead API is disabled")
        return

    host = os.getenv("LEAD_API_HOST", "127.0.0.1").strip() or "127.0.0.1"
    raw_port = os.getenv("PORT") or os.getenv("LEAD_API_PORT", "8000")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError(f"Invalid API port: {raw_port}") from exc

    def run_server() -> None:
        import uvicorn
        from lead_api import app as api_app

        uvicorn.run(api_app, host=host, port=port, log_level="info")

    thread = threading.Thread(target=run_server, name="satmakon-lead-api", daemon=True)
    thread.start()
    logger.info("Website lead API: http://%s:%s/api/leads", host, port)


def main() -> None:
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN topilmadi")
    if not ADMIN_GROUP_CHAT_ID:
        raise ValueError("ADMIN_GROUP_CHAT_ID topilmadi")
    try:
        int(ADMIN_GROUP_CHAT_ID)
    except ValueError as exc:
        raise ValueError("ADMIN_GROUP_CHAT_ID noto'g'ri") from exc

    if not DEEPSEEK_API_KEY:
        logger.warning("DEEPSEEK_API_KEY is missing: running in lead-collection fallback mode")

    start_lead_api()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(CommandHandler("chatid", chatid))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            handle_message,
        )
    )

    logger.info("Admin group chat ID: %s", ADMIN_GROUP_CHAT_ID)
    logger.info("SATMakon Bot ishga tushdi%s...", " (DeepSeek)" if client else " (fallback)")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()

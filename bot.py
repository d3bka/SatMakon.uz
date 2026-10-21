import os
import re
import json
import asyncio
import logging
import threading
from collections import defaultdict, deque
from difflib import SequenceMatcher
from datetime import date, timedelta
from pathlib import Path
from time import monotonic

from dotenv import load_dotenv
from openai import AsyncOpenAI
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from prompts import SYSTEM_PROMPT
from sheets import log_message, log_visit

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
ADMIN_GROUP_CHAT_ID = os.getenv("ADMIN_GROUP_CHAT_ID", "").strip()
ADMIN_USER_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_USER_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
MEMORY_FILE = DATA_DIR / "memory.json"
PHONES_FILE = DATA_DIR / "phones.json"
NOTIFIED_FILE = DATA_DIR / "notified.json"
SESSIONS_FILE = DATA_DIR / "sessions.json"

MAX_HISTORY_MESSAGES = 30
MAX_USER_MESSAGE_CHARS = 2000
RATE_LIMIT_REQUESTS = 10
RATE_LIMIT_WINDOW_SECONDS = 60
DEBOUNCE_SECONDS = 1.2

STATE_IDLE = "idle"
STATE_WAITING_CITY = "waiting_city"
STATE_WAITING_BRANCH = "waiting_branch"
STATE_WAITING_PHONE = "waiting_phone"
STATE_CONFIRM_CITY = "confirm_city"
STATE_CONFIRM_BRANCH = "confirm_branch"

LANG_UZ = "uz"
LANG_UZ_CYR = "uz_cyr"
LANG_RU = "ru"
LANG_EN = "en"

BRANCHES = {
    "novza": "https://yandex.uz/maps/-/CPSf6Tmj",
    "minor": "https://yandex.uz/maps/-/CPSfbZPb",
    "bukhara": "https://yandex.uz/maps/?pt=64.43023,39.76359&z=17&l=map",
    "gulistan": "https://maps.app.goo.gl/F7xSBxxiSqXPbN5i9",
    "navoi": "https://maps.app.goo.gl/F6LmXdmfCEARyRbg9",
}

CITY_ALIASES = {
    "tashkent": {
        "label": "Toshkent",
        "aliases": ["toshkent", "tashkent", "ташкент", "тошкент"],
    },
    "bukhara": {
        "label": "Buxoro",
        "aliases": ["buxoro", "bukhara", "buhoro", "бухоро", "бухара"],
    },
    "gulistan": {
        "label": "Guliston",
        "aliases": ["guliston", "gulistan", "sirdaryo", "гулистон", "гулистан", "сирдарё", "сирдарья"],
    },
    "navoi": {
        "label": "Navoiy",
        "aliases": ["navoiy", "navoi", "навоий", "навои"],
    },
    "other": {
        "label": "Samarqand",
        "aliases": [
            "samarqand", "samarkand", "самарканд",
            "fargona", "fergana", "фергана",
            "andijon", "andijan", "андижан",
            "namangan", "наманган",
            "qarshi", "karshi", "карши",
            "qashqadaryo", "кашқадарё", "кашкадарья",
            "surxondaryo", "сурхондарё", "сурхандарья",
            "termiz", "termez", "термез",
            "xorazm", "khorezm", "хорезм",
            "urgench", "urganch", "ургенч",
            "jizzax", "djizzakh", "джизак",
            "nukus", "нукус",
            "kokand", "qoqon", "коканд",
        ],
    },
}

BRANCH_ALIASES = {
    "novza": {
        "label": "Novza",
        "aliases": ["novza", "новза"],
    },
    "minor": {
        "label": "Minor",
        "aliases": ["minor", "минор"],
    },
}

COURSE_ALIASES = {
    "foundation": {
        "label": "Foundation",
        "aliases": ["foundation", "faundation", "foundatoin", "foundtion", "фаундейшн", "фаундэйшн"],
    },
    "pre_sat": {
        "label": "Pre-SAT",
        "aliases": ["pre sat", "pre-sat", "presat", "pre_sat", "пре сат", "пре-сат"],
    },
    "sat": {
        "label": "SAT",
        "aliases": ["sat", "сат"],
    },
    "ap_calculus": {
        "label": "AP Calculus",
        "aliases": ["ap calculus", "ap calc", "apcalculus", "ап калькулус", "ап calculus"],
    },
}

FUZZY_AUTO_THRESHOLD = 0.86
FUZZY_CONFIRM_THRESHOLD = 0.64
FUZZY_MIN_TOKEN_LEN = 4

PHONE = "+998 95 191 16 00"
ADMIN_USERNAME = "@satmakon_novzaa"

TEXTS = {
    LANG_UZ: {
        "greeting": "Assalomu alaykum. SATMakon o'quv markazining virtual konsultanti Ziyodaman. Labbay?",
        "ask_city": "Albatta, qaysi shaharda yashaysiz?",
        "city_retry": "Shahar nomini biroz to'liqroq yozib yuboring 😊 Masalan: Toshkent, Samarqand, Buxoro.",
        "confirm_city": "{city}ni nazarda tutdingizmi? 😊 Ha yoki yo'q deb yozing.",
        "ask_branch": "Toshkentda 2 ta filialimiz bor: Novza va Minor. Qaysi biri sizga qulay?",
        "branch_retry": "Filial nomini biroz aniqroq yozing 😊 Novza yoki Minor.",
        "confirm_branch": "{branch} filialini nazarda tutdingizmi? 😊 Ha yoki yo'q deb yozing.",
        "ask_phone": "Telefon raqamingizni qoldirsangiz, administrator siz bilan bog'lanadi.",
        "phone_retry": "Telefon raqamini +998 XX XXX XX XX formatida yuboring yoki Telegram kontaktini ulashing.",
        "phone_done": "Rahmat, raqamingiz qabul qilindi. Kutib qolamiz! 😊",
        "phone_cancelled": "Mayli, telefon raqamini qoldirmasangiz ham bo'ladi. Savolingiz bo'lsa, yozavering.",
        "too_long": "Xabaringiz juda uzun. Iltimos, savolni 2000 belgidan qisqaroq yuboring.",
        "rate_limit": "Juda ko'p xabar yuborildi. Bir ozdan keyin yana yozib ko'ring.",
        "unsupported": "Hozircha matn yoki Telegram kontaktini qabul qila olaman. Savolingizni matn ko'rinishida yuboring.",
        "ai_down": f"AI konsultant vaqtincha javob bera olmayapti. Admin bilan bog'lanishingiz mumkin: {ADMIN_USERNAME}, {PHONE} (09:00–20:00).",
        "no_branch": f"Sizning shahringizda hozircha filialimiz yo'q, lekin onlayn formatda o'qishingiz mumkin.||Bepul onlayn konsultatsiya va kursga yozilish uchun admin: {ADMIN_USERNAME}||{PHONE} (09:00–20:00)",
        "location_intro": "Mana o'quv markaz lokatsiyasi. Markazda bepul test topshirib, sizga mos guruhni aniqlashingiz mumkin.||{url}||Telefon raqamingizni qoldirsangiz, administrator siz bilan bog'lanadi.",
        "locations_intro": "Novza va Minor filiallarining lokatsiyalari:||{url1}||{url2}||Telefon raqamingizni qoldirsangiz, administrator siz bilan bog'lanadi.",
        "foundation": "Siz aytgan darajaga qaraganda, SAT yoki Pre-SATdan oldin Foundation kursi mosroq bo'ladi. U bazaviy ingliz tilini mustahkamlash uchun mo'ljallangan.||Qaysi shaharda yashaysiz?",
    },
    LANG_UZ_CYR: {
        "greeting": "Ассалому алайкум. SATMakon ўқув марказининг виртуал консультанти Зиёдаман. Лаббай?",
        "ask_city": "Албатта, қайси шаҳарда яшайсиз?",
        "city_retry": "Шаҳар номини бироз тўлиқроқ ёзиб юборинг 😊 Масалан: Тошкент, Самарқанд, Бухоро.",
        "confirm_city": "{city}ни назарда тутдингизми? 😊 Ҳа ёки йўқ деб ёзинг.",
        "ask_branch": "Тошкентда 2 та филиалимиз бор: Novza ва Minor. Қайси бири сизга қулай?",
        "branch_retry": "Филиал номини бироз аниқроқ ёзинг 😊 Novza ёки Minor.",
        "confirm_branch": "{branch} филиалини назарда тутдингизми? 😊 Ҳа ёки йўқ деб ёзинг.",
        "ask_phone": "Телефон рақамингизни қолдирсангиз, администратор сиз билан боғланади.",
        "phone_retry": "Телефон рақамини +998 XX XXX XX XX форматида юборинг ёки Telegram контактини улашинг.",
        "phone_done": "Раҳмат, рақамингиз қабул қилинди. Кутиб қоламиз! 😊",
        "phone_cancelled": "Майли, телефон рақамини қолдирмасангиз ҳам бўлади. Саволингиз бўлса, ёзаверинг.",
        "too_long": "Хабарингиз жуда узун. Илтимос, саволни 2000 белгидан қисқароқ юборинг.",
        "rate_limit": "Жуда кўп хабар юборилди. Бироздан кейин яна ёзиб кўринг.",
        "unsupported": "Ҳозирча матн ёки Telegram контактини қабул қила оламан. Саволингизни матн кўринишида юборинг.",
        "ai_down": f"AI консультант вақтинча жавоб бера олмаяпти. Админ билан боғланишингиз мумкин: {ADMIN_USERNAME}, {PHONE} (09:00–20:00).",
        "no_branch": f"Сизнинг шаҳрингизда ҳозирча филиалимиз йўқ, лекин онлайн форматда ўқишингиз мумкин.||Бепул онлайн консультация ва курсга ёзилиш учун админ: {ADMIN_USERNAME}||{PHONE} (09:00–20:00)",
        "location_intro": "Мана ўқув марказ локацияси. Марказда бепул тест топшириб, сизга мос гуруҳни аниқлашингиз мумкин.||{url}||Телефон рақамингизни қолдирсангиз, администратор сиз билан боғланади.",
        "locations_intro": "Novza ва Minor филиалларининг локациялари:||{url1}||{url2}||Телефон рақамингизни қолдирсангиз, администратор сиз билан боғланади.",
        "foundation": "Сиз айтган даражага қараганда, SAT ёки Pre-SATдан олдин Foundation курси мосроқ бўлади. У базавий инглиз тилини мустаҳкамлаш учун мўлжалланган.||Қайси шаҳарда яшайсиз?",
    },
    LANG_RU: {
        "greeting": "Здравствуйте. Я Зиёда, виртуальный консультант учебного центра SATMakon. Чем могу помочь?",
        "ask_city": "Конечно. В каком городе вы живёте?",
        "city_retry": "Напишите название города чуть полнее 😊 Например: Ташкент, Самарканд, Бухара.",
        "confirm_city": "Вы имели в виду {city}? 😊 Ответьте да или нет.",
        "ask_branch": "В Ташкенте у нас 2 филиала: Novza и Minor. Какой вам удобнее?",
        "branch_retry": "Напишите филиал чуть точнее 😊 Novza или Minor.",
        "confirm_branch": "Вы имели в виду филиал {branch}? 😊 Ответьте да или нет.",
        "ask_phone": "Оставьте номер телефона, и администратор свяжется с вами.",
        "phone_retry": "Отправьте номер в формате +998 XX XXX XX XX или поделитесь контактом Telegram.",
        "phone_done": "Спасибо, номер получен. Будем ждать вас! 😊",
        "phone_cancelled": "Хорошо, номер оставлять необязательно. Если есть вопрос, напишите его.",
        "too_long": "Сообщение слишком длинное. Пожалуйста, отправьте вопрос короче 2000 символов.",
        "rate_limit": "Слишком много сообщений за короткое время. Попробуйте снова немного позже.",
        "unsupported": "Пока я принимаю текст и Telegram-контакты. Отправьте вопрос текстом.",
        "ai_down": f"AI-консультант временно недоступен. Можно связаться с администратором: {ADMIN_USERNAME}, {PHONE} (09:00–20:00).",
        "no_branch": f"В вашем городе пока нет филиала, но можно заниматься онлайн.||Для бесплатной онлайн-консультации и записи: {ADMIN_USERNAME}||{PHONE} (09:00–20:00)",
        "location_intro": "Вот локация учебного центра. В центре можно пройти бесплатный тест и подобрать подходящую группу.||{url}||Оставьте номер телефона, и администратор свяжется с вами.",
        "locations_intro": "Локации филиалов Novza и Minor:||{url1}||{url2}||Оставьте номер телефона, и администратор свяжется с вами.",
        "foundation": "По указанному уровню вам разумнее начать с Foundation, а не с SAT или Pre-SAT. Курс укрепляет базовый английский.||В каком городе вы живёте?",
    },
    LANG_EN: {
        "greeting": "Hello. I'm Ziyoda, SATMakon's virtual consultant. How can I help?",
        "ask_city": "Sure. Which city do you live in?",
        "city_retry": "Please type the city name a little more fully 😊 For example: Tashkent, Samarkand, Bukhara.",
        "confirm_city": "Did you mean {city}? 😊 Reply yes or no.",
        "ask_branch": "We have two branches in Tashkent: Novza and Minor. Which one is more convenient for you?",
        "branch_retry": "Please type the branch name a little more clearly 😊 Novza or Minor.",
        "confirm_branch": "Did you mean the {branch} branch? 😊 Reply yes or no.",
        "ask_phone": "Leave your phone number and an administrator will contact you.",
        "phone_retry": "Send your number in +998 XX XXX XX XX format or share your Telegram contact.",
        "phone_done": "Thank you, your number has been received. We look forward to seeing you! 😊",
        "phone_cancelled": "No problem, leaving a phone number is optional. Send any question you have.",
        "too_long": "Your message is too long. Please keep the question under 2000 characters.",
        "rate_limit": "Too many messages in a short period. Please try again a little later.",
        "unsupported": "For now I can accept text and Telegram contacts. Please send your question as text.",
        "ai_down": f"The AI consultant is temporarily unavailable. You can contact the administrator at {ADMIN_USERNAME} or {PHONE} (09:00–20:00).",
        "no_branch": f"We do not have a branch in your city yet, but you can study online.||For a free online consultation and enrollment: {ADMIN_USERNAME}||{PHONE} (09:00–20:00)",
        "location_intro": "Here is the centre location. You can take a free placement test there and be matched with a suitable group.||{url}||Leave your phone number and an administrator will contact you.",
        "locations_intro": "Here are the Novza and Minor branch locations:||{url1}||{url2}||Leave your phone number and an administrator will contact you.",
        "foundation": "Based on the level you described, Foundation is a better starting point than SAT or Pre-SAT. It is designed to strengthen basic English.||Which city do you live in?",
    },
}

client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com/v1") if DEEPSEEK_API_KEY else None


def _read_json(path: Path, default):
    try:
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                return json.loads(content)
    except Exception as exc:
        logger.warning("Could not read %s: %s", path.name, exc)
    return default


def _write_json(path: Path, value) -> None:
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        logger.warning("Could not write %s: %s", path.name, exc)


def _migrate_legacy_json_files() -> None:
    for filename, target in (
        ("memory.json", MEMORY_FILE),
        ("phones.json", PHONES_FILE),
        ("notified.json", NOTIFIED_FILE),
    ):
        legacy = BASE_DIR / filename
        if not legacy.exists() or target.exists():
            continue
        try:
            legacy.replace(target)
            logger.info("Migrated legacy %s into data/", filename)
        except OSError as exc:
            logger.warning("Could not migrate %s: %s", filename, exc)


def _ensure_data_files() -> None:
    for path in (MEMORY_FILE, PHONES_FILE, NOTIFIED_FILE, SESSIONS_FILE):
        if not path.exists():
            _write_json(path, {})


_migrate_legacy_json_files()
_ensure_data_files()
histories: dict[int, list] = {int(k): v for k, v in _read_json(MEMORY_FILE, {}).items()}
user_phones: dict[int, str] = {int(k): v for k, v in _read_json(PHONES_FILE, {}).items()}
notified_visits: dict[int, str] = {int(k): v for k, v in _read_json(NOTIFIED_FILE, {}).items()}
sessions: dict[int, dict] = {int(k): v for k, v in _read_json(SESSIONS_FILE, {}).items()}

pending_messages: dict[int, list[str]] = {}
last_message_time: dict[int, float] = {}
pending_tasks: dict[int, asyncio.Task] = {}
rate_windows: dict[int, deque] = defaultdict(deque)


def save_histories() -> None:
    trimmed = {str(k): v[-MAX_HISTORY_MESSAGES:] for k, v in histories.items()}
    _write_json(MEMORY_FILE, trimmed)


def save_phones() -> None:
    _write_json(PHONES_FILE, {str(k): v for k, v in user_phones.items()})


def save_notified() -> None:
    _write_json(NOTIFIED_FILE, {str(k): v for k, v in notified_visits.items()})


def save_sessions() -> None:
    _write_json(SESSIONS_FILE, {str(k): v for k, v in sessions.items()})


def get_session(user_id: int) -> dict:
    session = sessions.setdefault(user_id, {"state": STATE_IDLE, "language": LANG_UZ})
    session.setdefault("state", STATE_IDLE)
    session.setdefault("language", LANG_UZ)
    return session


def reset_user_state(user_id: int) -> None:
    histories.pop(user_id, None)
    sessions[user_id] = {"state": STATE_IDLE, "language": LANG_UZ}
    notified_visits.pop(user_id, None)
    pending_messages.pop(user_id, None)
    last_message_time.pop(user_id, None)
    task = pending_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()
    save_histories()
    save_sessions()
    save_notified()


def detect_language(text: str, previous: str = LANG_UZ) -> str:
    value = (text or "").strip().lower()
    if not value:
        return previous

    meaningful = re.findall(r"[a-zа-яёўқғҳ']+", value, flags=re.I)
    if len(meaningful) <= 1 and (value in {"ok", "okay", "ha", "xa", "да", "yes", "rahmat", "спасибо", "thanks"} or value.isdigit()):
        return previous

    if re.search(r"[ўқғҳ]", value) or re.search(r"\b(ёзил|нарх|қанча|қайси|шаҳар|ўқув|бўладими)\w*", value):
        return LANG_UZ_CYR
    if re.search(r"\b(здравствуйте|сколько|хочу|цена|стоит|курс|филиал|адрес|учител|позвон|номер|можно|спасибо|город)\w*", value):
        return LANG_RU
    if re.search(r"\b(hello|hi|how|what|price|cost|want|course|branch|teacher|phone|english|math|enroll|city|thanks)\b", value):
        return LANG_EN
    return LANG_UZ if re.search(r"[a-z]", value) else previous


def split_reply(text: str) -> list[str]:
    return [clean_reply(part) for part in text.split("||") if clean_reply(part)]


def clean_reply(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.S)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", text, flags=re.S)
    text = re.sub(r"`(.+?)`", r"\1", text, flags=re.S)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"[Mm]akon\.ai\s*(platformasi|orqali|da|bilan)?\s*", "", text).strip()
    # Fix only the known bad phrase; do not globally mutate grammatically valid "Sizni".
    text = re.sub(r"\bSizni qanday yordam bera olaman\??", "Sizga qanday yordam bera olaman?", text, flags=re.I)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_phone(text: str) -> str | None:
    if not text:
        return None
    # Match a plausible Uzbek phone embedded in prose; do not accept arbitrary 7-digit dates/IDs.
    match = re.search(r"(?<!\d)(?:\+?998[\s\-()]*)?(\d{2})[\s\-()]*(\d{3})[\s\-()]*(\d{2})[\s\-()]*(\d{2})(?!\d)", text)
    if not match:
        return None
    local = "".join(match.groups())
    if len(local) != 9:
        return None
    return f"+998 {local[:2]} {local[2:5]} {local[5:7]} {local[7:]}"


def is_phone_number(text: str) -> bool:
    return extract_phone(text) is not None


def is_cancel(text: str) -> bool:
    lower = (text or "").lower().strip()
    return bool(re.search(r"\b(yo['’]?q|kerak emas|qoldirmayman|отмена|не хочу|не буду|не надо|cancel|no thanks|don't want)\b", lower))


def is_question(text: str) -> bool:
    value = (text or "").strip().lower()
    return "?" in value or bool(re.search(r"\b(narx|qancha|qayer|qachon|necha|bormi|сколько|где|когда|есть|можно|how|what|where|when|do you|can i)\b", value))


def enrollment_intent(text: str) -> bool:
    value = (text or "").lower()
    if is_cancel(value):
        return False
    negative = re.search(r"\b(kelmayman|bormayman|yozilmayman|не приду|не приеду|не буду приходить|not coming|won't come|do not want to enroll)\b", value)
    if negative:
        return False
    return bool(re.search(
        r"\b(yozilmoqchiman|yozilaman|kelmoqchiman|kelaman|boraman|kelsam bo['’]?ladimi|записаться|хочу записаться|приду|приеду|хочу прийти|enroll|sign up|i(?:'|’)ll come|i will come|want to come)\b",
        value,
    ))


def low_english_level(text: str) -> bool:
    value = (text or "").lower().replace("ё", "е")
    patterns = [
        r"\bpre\b", r"\ba1\b", r"\bbeginner\b", r"\belementary\b",
        r"ingliz\w*\s+(juda\s+)?past", r"english\s+(is\s+)?(very\s+)?weak",
        r"английск\w*\s+(очень\s+)?слаб", r"бошланғич", r"бошловчи",
        r"mez\s*5", r"мез\s*5", r"5\s*(?:class|klass|класс)",
    ]
    return any(re.search(pattern, value) for pattern in patterns)


def _normalize_match_text(text: str) -> str:
    value = (text or "").lower().replace("ё", "е")
    value = value.replace("’", "'").replace("`", "'")
    value = re.sub(r"[_/\\|,;:!?()\[\]{}]+", " ", value)
    value = re.sub(r"[-–—]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _levenshtein_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if len(a) > len(b):
        a, b = b, a
    previous = list(range(len(a) + 1))
    for i, char_b in enumerate(b, start=1):
        current = [i]
        for j, char_a in enumerate(a, start=1):
            insert_cost = current[j - 1] + 1
            delete_cost = previous[j] + 1
            replace_cost = previous[j - 1] + (char_a != char_b)
            current.append(min(insert_cost, delete_cost, replace_cost))
        previous = current
    return previous[-1]


def _candidate_chunks(text: str) -> list[str]:
    value = _normalize_match_text(text)
    if not value:
        return []
    words = [word for word in value.split() if word]
    chunks = list(words)
    chunks.extend(" ".join(words[i:i + 2]) for i in range(len(words) - 1))
    if len(words) <= 3:
        chunks.append(value)
    return list(dict.fromkeys(chunks))


def fuzzy_alias_match(text: str, catalog: dict) -> dict | None:
    """
    Exact match -> prefix -> typo-tolerant edit distance -> fuzzy similarity.
    Short fragments are never auto-accepted.
    """
    chunks = _candidate_chunks(text)
    if not chunks:
        return None

    ranked = []
    for key, info in catalog.items():
        for alias in info["aliases"]:
            normalized_alias = _normalize_match_text(alias)
            for chunk in chunks:
                if not chunk:
                    continue

                if chunk == normalized_alias:
                    score = 1.0
                else:
                    ratio = SequenceMatcher(None, chunk, normalized_alias).ratio()
                    distance = _levenshtein_distance(chunk, normalized_alias)

                    if len(chunk) >= 5 and normalized_alias.startswith(chunk):
                        score = max(ratio, 0.92)
                    elif len(chunk) >= 5 and distance <= 1:
                        score = max(ratio, 0.96)
                    elif len(chunk) >= 5 and distance <= 2 and ratio >= 0.72:
                        score = max(ratio, 0.90)
                    else:
                        score = ratio

                ranked.append((score, key, info["label"], normalized_alias, chunk))

    if not ranked:
        return None

    ranked.sort(key=lambda row: row[0], reverse=True)
    best_score, best_key, best_label, best_alias, best_chunk = ranked[0]

    runner_score = 0.0
    for row in ranked[1:]:
        if row[1] != best_key:
            runner_score = row[0]
            break

    if len(best_chunk) < FUZZY_MIN_TOKEN_LEN and best_score < 1.0:
        return None
    if best_score < FUZZY_CONFIRM_THRESHOLD:
        return None

    ambiguous = runner_score >= best_score - 0.07
    status = "auto" if best_score >= FUZZY_AUTO_THRESHOLD and not ambiguous else "confirm"

    display_label = best_alias.title() if best_key == "other" else best_label
    return {
        "key": best_key,
        "label": display_label,
        "alias": best_alias,
        "score": round(best_score, 4),
        "status": status,
    }


def match_city(text: str) -> dict | None:
    return fuzzy_alias_match(text, CITY_ALIASES)


def detect_city(text: str) -> str | None:
    match = match_city(text)
    if match and match["status"] == "auto":
        return match["key"]
    return None


def match_branch(text: str) -> dict | None:
    value = _normalize_match_text(text)
    if re.search(r"\b(farqi yo['’]?q|bilmayman|bilmadim|без разницы|не знаю|either|any|doesn'?t matter)\b", value):
        return {"key": "either", "label": "Novza / Minor", "alias": value, "score": 1.0, "status": "auto"}
    return fuzzy_alias_match(text, BRANCH_ALIASES)


def detect_branch(text: str) -> str | None:
    match = match_branch(text)
    if match and match["status"] == "auto":
        return match["key"]
    return None


def detect_course(text: str) -> str | None:
    match = fuzzy_alias_match(text, COURSE_ALIASES)
    if match and match["status"] == "auto":
        return match["key"]
    return None


def is_yes(text: str) -> bool:
    value = _normalize_match_text(text)
    return bool(re.fullmatch(r"(ha+|xa+|yes+|yep|yeah|да+|ага|конечно|албатта|ҳа+)", value))


def is_no(text: str) -> bool:
    value = _normalize_match_text(text)
    return bool(re.fullmatch(r"(yo['’]?q|yoq|no+|нет+|неа|йўқ)", value))



def faq_reply(text: str, lang: str) -> str | None:
    value = (text or "").lower()

    if re.search(r"\b(telefon raqamingiz|telefoningiz|tel bering|qo['’]?ng['’]?iroq|номер телефона|позвон|phone number|can i call|call you)\b", value):
        return {
            LANG_UZ: f"Albatta, {PHONE} ga qo'ng'iroq qilishingiz mumkin. Ish vaqtimiz 09:00–20:00.",
            LANG_UZ_CYR: f"Албатта, {PHONE} га қўнғироқ қилишингиз мумкин. Иш вақтимиз 09:00–20:00.",
            LANG_RU: f"Конечно, вы можете позвонить по номеру {PHONE}. Мы работаем с 09:00 до 20:00.",
            LANG_EN: f"Sure, you can call {PHONE}. Our working hours are 09:00–20:00.",
        }[lang]

    if re.search(r"\b(admin|administrator|operator|menejer|inson|real odam|админ|оператор|менеджер|живой человек|person|human)\b", value):
        return {
            LANG_UZ: f"Admin bilan to'g'ridan-to'g'ri bog'lanishingiz mumkin:||{ADMIN_USERNAME}||{PHONE} (09:00–20:00)",
            LANG_UZ_CYR: f"Админ билан тўғридан-тўғри боғланишингиз мумкин:||{ADMIN_USERNAME}||{PHONE} (09:00–20:00)",
            LANG_RU: f"Можно связаться с администратором напрямую:||{ADMIN_USERNAME}||{PHONE} (09:00–20:00)",
            LANG_EN: f"You can contact an administrator directly:||{ADMIN_USERNAME}||{PHONE} (09:00–20:00)",
        }[lang]

    if re.search(r"\b(ish vaqti|soat nechigacha|работаете|время работы|working hours|what time.*open)\b", value):
        return {
            LANG_UZ: "Har kuni 09:00 dan 20:00 gacha ishlaymiz.",
            LANG_UZ_CYR: "Ҳар куни 09:00 дан 20:00 гача ишлаймиз.",
            LANG_RU: "Мы работаем каждый день с 09:00 до 20:00.",
            LANG_EN: "We are open every day from 09:00 to 20:00.",
        }[lang]

    if re.search(r"\b(instagram|telegram kanal|youtube|2gis)\b", value):
        if "instagram" in value:
            return "Instagram: @sat_makon_uz — https://instagram.com/sat_makon_uz"
        if "youtube" in value:
            return "YouTube: https://youtube.com/@sat_makon_uz"
        if "2gis" in value:
            return "2GIS: https://2gis.uz/uz/tashkent/firm/70000001110887440"
        return "Telegram: @sat_makon — https://t.me/sat_makon"

    if re.search(r"\b(narx|qancha|цена|сколько стоит|price|cost)\b", value):
        course = detect_course(value)
        if course == "foundation":
            return {
                LANG_UZ: f"Foundation narxi bo'yicha noto'g'ri ma'lumot bermaslik uchun admin bilan aniqlashtiring: {ADMIN_USERNAME} yoki {PHONE}.",
                LANG_UZ_CYR: f"Foundation нархи бўйича нотўғри маълумот бермаслик учун админ билан аниқлаштиринг: {ADMIN_USERNAME} ёки {PHONE}.",
                LANG_RU: f"Чтобы не назвать неверную цену Foundation, уточните у администратора: {ADMIN_USERNAME} или {PHONE}.",
                LANG_EN: f"To avoid giving an incorrect Foundation price, please confirm it with the admin: {ADMIN_USERNAME} or {PHONE}.",
            }[lang]
        if course == "ap_calculus" or "ap" in value or "calculus" in value:
            return {
                LANG_UZ: "AP Calculus narxi 1,000,000 so'm/oy.",
                LANG_UZ_CYR: "AP Calculus нархи ойига 1,000,000 сўм.",
                LANG_RU: "AP Calculus стоит 1 000 000 сум в месяц.",
                LANG_EN: "AP Calculus costs 1,000,000 UZS per month.",
            }[lang]
        return {
            LANG_UZ: "SAT va Pre-SAT: 1 fan — 900,000 so'm/oy, 2 fan — 1,700,000 so'm/oy.",
            LANG_UZ_CYR: "SAT ва Pre-SAT: 1 фан — ойига 900,000 сўм, 2 фан — ойига 1,700,000 сўм.",
            LANG_RU: "SAT и Pre-SAT: 1 предмет — 900 000 сум/месяц, 2 предмета — 1 700 000 сум/месяц.",
            LANG_EN: "SAT and Pre-SAT: one subject is 900,000 UZS/month; two subjects are 1,700,000 UZS/month.",
        }[lang]

    return None


def _rate_limited(user_id: int) -> bool:
    now = monotonic()
    bucket = rate_windows[user_id]
    while bucket and now - bucket[0] > RATE_LIMIT_WINDOW_SECONDS:
        bucket.popleft()
    if len(bucket) >= RATE_LIMIT_REQUESTS:
        return True
    bucket.append(now)
    return False


async def get_reply(user_id: int, user_message: str, lang: str) -> str:
    if client is None:
        raise RuntimeError("DeepSeek is not configured")

    history = histories.get(user_id, [])[-MAX_HISTORY_MESSAGES:]
    history.append({"role": "user", "content": user_message})
    language_instruction = {
        LANG_UZ: "Reply in Uzbek Latin.",
        LANG_UZ_CYR: "Reply in Uzbek Cyrillic.",
        LANG_RU: "Reply in Russian.",
        LANG_EN: "Reply in English.",
    }[lang]

    last_error = None
    for attempt in range(3):
        try:
            response = await client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT + "\n\n" + language_instruction},
                    *history[-20:],
                ],
                temperature=0.2,
                max_tokens=500,
            )
            raw_reply = response.choices[0].message.content.strip()
            cleaned_parts = split_reply(raw_reply)
            reply = "||".join(cleaned_parts) if cleaned_parts else clean_reply(raw_reply)
            history.append({"role": "assistant", "content": reply})
            histories[user_id] = history[-MAX_HISTORY_MESSAGES:]
            save_histories()
            return reply
        except Exception as exc:
            last_error = exc
            logger.warning("DeepSeek attempt %s failed: %s", attempt + 1, exc)
            error_text = str(exc).lower()
            if "402" in error_text or "insufficient balance" in error_text:
                break
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
    histories[user_id] = history[-MAX_HISTORY_MESSAGES:]
    save_histories()
    raise last_error or RuntimeError("DeepSeek request failed")


def _extract_visit_datetime(history: list) -> tuple[str, str]:
    user_text = " ".join(item.get("content", "") for item in history[-12:] if item.get("role") == "user").lower()
    visit_date = ""
    today = date.today()
    if "indinga" in user_text or "indin" in user_text:
        visit_date = (today + timedelta(days=2)).isoformat()
    elif "ertaga" in user_text or "завтра" in user_text or "tomorrow" in user_text:
        visit_date = (today + timedelta(days=1)).isoformat()
    elif "bugun" in user_text or "сегодня" in user_text or "today" in user_text:
        visit_date = today.isoformat()
    else:
        match_date = re.search(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b", user_text)
        if match_date:
            try:
                visit_date = date(int(match_date[1]), int(match_date[2]), int(match_date[3])).isoformat()
            except ValueError:
                pass

    visit_time = ""
    match_time = re.search(r"(?:soat|at|в)\s*(\d{1,2})(?::(\d{2}))?", user_text)
    if match_time:
        hour = int(match_time[1])
        minute = int(match_time[2] or 0)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            if 1 <= hour <= 8 and not re.search(r"ertalab|утр|am\b", user_text):
                hour += 12
            visit_time = f"{hour:02d}:{minute:02d}"
    return visit_date, visit_time


def _format_admin_message(tg_user, phone: str, session: dict, history: list) -> str:
    name = tg_user.full_name or "Mijoz"
    uname = f"@{tg_user.username}" if tg_user.username else ""
    header = f"{name} {uname}".strip()
    visit_date, visit_time = _extract_visit_datetime(history)
    branch = session.get("branch") or session.get("city") or "aniqlanmagan"
    latest_request = next((x.get("content", "") for x in reversed(history) if x.get("role") == "user"), "")
    when = visit_date or "aniqlanmagan"
    if visit_time:
        when += f" {visit_time}"
    return (
        "🆕 Yangi lead / tashrif\n\n"
        f"👤 {header}\n"
        f"📞 {phone}\n"
        f"📍 Filial/shahar: {branch}\n"
        f"📅 Sana: {when}\n"
        f"💬 So'rov: {latest_request[:500] or 'Telegram murojaati'}"
    )


async def notify_admin_group(context, tg_user, phone: str, session: dict, history: list) -> None:
    if not ADMIN_GROUP_CHAT_ID:
        return
    try:
        chat_id = int(ADMIN_GROUP_CHAT_ID)
    except ValueError:
        logger.warning("Invalid ADMIN_GROUP_CHAT_ID: %s", ADMIN_GROUP_CHAT_ID)
        return

    key = f"{phone}|{session.get('city','')}|{session.get('branch','')}"
    if notified_visits.get(tg_user.id) == key:
        return
    try:
        await context.bot.send_message(chat_id=chat_id, text=_format_admin_message(tg_user, phone, session, history))
        notified_visits[tg_user.id] = key
        save_notified()
    except Exception as exc:
        logger.warning("Admin group notify failed: %s", exc)


async def finalize_phone(tg_user, user_id: int, phone: str, context) -> None:
    session = get_session(user_id)
    user_phones[user_id] = phone
    save_phones()
    session["state"] = STATE_IDLE
    save_sessions()

    history = histories.get(user_id, [])
    visit_date, visit_time = _extract_visit_datetime(history)
    try:
        await asyncio.to_thread(
            log_visit,
            username=tg_user.username,
            full_name=tg_user.full_name,
            user_id=tg_user.id,
            phone=phone,
            visit_date=visit_date,
            visit_time=visit_time,
            topic=session.get("branch") or session.get("city") or "Telegram murojaati",
        )
    except Exception as exc:
        logger.warning("log_visit error: %s", exc)
    await notify_admin_group(context, tg_user, phone, session, history)


async def send_reply(update: Update, text: str) -> None:
    parts = split_reply(text)
    for index, part in enumerate(parts):
        await update.message.reply_text(part)
        if index < len(parts) - 1:
            await asyncio.sleep(0.35)


def record_exchange(user_id: int, user_text: str, bot_reply: str) -> None:
    history = histories.setdefault(user_id, [])
    history.extend([
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": bot_reply},
    ])
    histories[user_id] = history[-MAX_HISTORY_MESSAGES:]
    save_histories()


async def respond(update: Update, user_id: int, user_text: str, bot_reply: str, *, log: bool = True) -> None:
    record_exchange(user_id, user_text, bot_reply)
    await send_reply(update, bot_reply)
    if log:
        await _log_message_async(update, user_text, bot_reply)


async def start(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    reset_user_state(user_id)
    await update.message.reply_text(TEXTS[LANG_UZ]["greeting"])


async def reset(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    previous = get_session(user_id).get("language", LANG_UZ)
    reset_user_state(user_id)
    sessions[user_id]["language"] = previous
    save_sessions()
    await update.message.reply_text(TEXTS[previous]["greeting"])


async def _accept_city_choice(update: Update, user_id: int, user_text: str, context, city: str, lang: str) -> None:
    session = get_session(user_id)
    session.pop("pending_city", None)
    session.pop("pending_city_label", None)
    session["city"] = city

    if city == "tashkent":
        session["state"] = STATE_WAITING_BRANCH
        save_sessions()
        await respond(update, user_id, user_text, TEXTS[lang]["ask_branch"])
        return

    if city == "other":
        session["state"] = STATE_IDLE
        save_sessions()
        await respond(update, user_id, user_text, TEXTS[lang]["no_branch"])
        return

    branch = {"bukhara": "bukhara", "gulistan": "gulistan", "navoi": "navoi"}[city]
    session.update({"branch": branch, "state": STATE_WAITING_PHONE})
    save_sessions()
    await respond(
        update,
        user_id,
        user_text,
        TEXTS[lang]["location_intro"].format(url=BRANCHES[branch]),
    )


async def _accept_branch_choice(update: Update, user_id: int, user_text: str, context, branch: str, lang: str) -> None:
    session = get_session(user_id)
    session.pop("pending_branch", None)
    session.pop("pending_branch_label", None)
    session["branch"] = branch
    session["state"] = STATE_WAITING_PHONE
    save_sessions()

    if branch == "either":
        reply = TEXTS[lang]["locations_intro"].format(
            url1=BRANCHES["novza"],
            url2=BRANCHES["minor"],
        )
    else:
        reply = TEXTS[lang]["location_intro"].format(url=BRANCHES[branch])

    await respond(update, user_id, user_text, reply)


async def process_user_message(user_id: int, combined_text: str, update: Update, context) -> None:
    session = get_session(user_id)
    lang = detect_language(combined_text, session.get("language", LANG_UZ))
    session["language"] = lang
    save_sessions()

    if len(combined_text) > MAX_USER_MESSAGE_CHARS:
        await update.message.reply_text(TEXTS[lang]["too_long"])
        return
    if _rate_limited(user_id):
        await update.message.reply_text(TEXTS[lang]["rate_limit"])
        return

    phone = extract_phone(combined_text)
    if session.get("state") == STATE_WAITING_PHONE:
        if phone:
            await finalize_phone(update.effective_user, user_id, phone, context)
            await respond(update, user_id, combined_text, TEXTS[lang]["phone_done"])
            return
        if is_cancel(combined_text):
            session["state"] = STATE_IDLE
            save_sessions()
            await respond(update, user_id, combined_text, TEXTS[lang]["phone_cancelled"])
            return
        if is_question(combined_text):
            session["state"] = STATE_IDLE
            save_sessions()
        else:
            await respond(update, user_id, combined_text, TEXTS[lang]["phone_retry"])
            return
    elif phone and enrollment_intent(" ".join(
        x.get("content", "")
        for x in histories.get(user_id, [])[-5:]
        if x.get("role") == "user"
    )):
        await finalize_phone(update.effective_user, user_id, phone, context)
        await respond(update, user_id, combined_text, TEXTS[lang]["phone_done"])
        return

    state = session.get("state", STATE_IDLE)

    # Medium-confidence city: accept yes/no, or let the user type a corrected city directly.
    if state == STATE_CONFIRM_CITY:
        pending_city = session.get("pending_city")
        pending_label = session.get("pending_city_label", "Toshkent")

        if is_yes(combined_text) and pending_city:
            await _accept_city_choice(update, user_id, combined_text, context, pending_city, lang)
            return

        if is_no(combined_text) or is_cancel(combined_text):
            session.pop("pending_city", None)
            session.pop("pending_city_label", None)
            session["state"] = STATE_WAITING_CITY
            save_sessions()
            await respond(update, user_id, combined_text, TEXTS[lang]["city_retry"])
            return

        revised = match_city(combined_text)
        if revised:
            if revised["status"] == "auto":
                await _accept_city_choice(update, user_id, combined_text, context, revised["key"], lang)
                return
            session["pending_city"] = revised["key"]
            session["pending_city_label"] = revised["label"]
            save_sessions()
            await respond(
                update,
                user_id,
                combined_text,
                TEXTS[lang]["confirm_city"].format(city=revised["label"]),
            )
            return

        if is_question(combined_text):
            session.pop("pending_city", None)
            session.pop("pending_city_label", None)
            session["state"] = STATE_IDLE
            save_sessions()
        else:
            await respond(
                update,
                user_id,
                combined_text,
                TEXTS[lang]["confirm_city"].format(city=pending_label),
            )
            return

    # Medium-confidence branch: same behavior as city confirmation.
    state = session.get("state", STATE_IDLE)
    if state == STATE_CONFIRM_BRANCH:
        pending_branch = session.get("pending_branch")
        pending_label = session.get("pending_branch_label", "Novza")

        if is_yes(combined_text) and pending_branch:
            await _accept_branch_choice(update, user_id, combined_text, context, pending_branch, lang)
            return

        if is_no(combined_text) or is_cancel(combined_text):
            session.pop("pending_branch", None)
            session.pop("pending_branch_label", None)
            session["state"] = STATE_WAITING_BRANCH
            save_sessions()
            await respond(update, user_id, combined_text, TEXTS[lang]["branch_retry"])
            return

        revised = match_branch(combined_text)
        if revised:
            if revised["status"] == "auto":
                await _accept_branch_choice(update, user_id, combined_text, context, revised["key"], lang)
                return
            session["pending_branch"] = revised["key"]
            session["pending_branch_label"] = revised["label"]
            save_sessions()
            await respond(
                update,
                user_id,
                combined_text,
                TEXTS[lang]["confirm_branch"].format(branch=revised["label"]),
            )
            return

        if is_question(combined_text):
            session.pop("pending_branch", None)
            session.pop("pending_branch_label", None)
            session["state"] = STATE_IDLE
            save_sessions()
        else:
            await respond(
                update,
                user_id,
                combined_text,
                TEXTS[lang]["confirm_branch"].format(branch=pending_label),
            )
            return

    if low_english_level(combined_text):
        session.update({"state": STATE_WAITING_CITY, "topic": "Foundation"})
        save_sessions()
        await respond(update, user_id, combined_text, TEXTS[lang]["foundation"])
        return

    state = session.get("state", STATE_IDLE)
    if state == STATE_WAITING_CITY:
        city_match = match_city(combined_text)

        if not city_match:
            if is_question(combined_text):
                session["state"] = STATE_IDLE
                save_sessions()
            else:
                await respond(update, user_id, combined_text, TEXTS[lang]["city_retry"])
                return
        elif city_match["status"] == "confirm":
            session["pending_city"] = city_match["key"]
            session["pending_city_label"] = city_match["label"]
            session["state"] = STATE_CONFIRM_CITY
            save_sessions()
            await respond(
                update,
                user_id,
                combined_text,
                TEXTS[lang]["confirm_city"].format(city=city_match["label"]),
            )
            return
        else:
            await _accept_city_choice(
                update,
                user_id,
                combined_text,
                context,
                city_match["key"],
                lang,
            )
            return

    state = session.get("state", STATE_IDLE)
    if state == STATE_WAITING_BRANCH:
        branch_match = match_branch(combined_text)

        if not branch_match:
            if is_question(combined_text):
                session["state"] = STATE_IDLE
                save_sessions()
            else:
                await respond(update, user_id, combined_text, TEXTS[lang]["branch_retry"])
                return
        elif branch_match["status"] == "confirm":
            session["pending_branch"] = branch_match["key"]
            session["pending_branch_label"] = branch_match["label"]
            session["state"] = STATE_CONFIRM_BRANCH
            save_sessions()
            await respond(
                update,
                user_id,
                combined_text,
                TEXTS[lang]["confirm_branch"].format(branch=branch_match["label"]),
            )
            return
        else:
            await _accept_branch_choice(
                update,
                user_id,
                combined_text,
                context,
                branch_match["key"],
                lang,
            )
            return

    if enrollment_intent(combined_text):
        session.update({"state": STATE_WAITING_CITY, "topic": "Enrollment"})
        save_sessions()
        await respond(update, user_id, combined_text, TEXTS[lang]["ask_city"])
        return

    quick = faq_reply(combined_text, lang)
    if quick:
        await respond(update, user_id, combined_text, quick)
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        reply = await get_reply(user_id, combined_text, lang)
    except Exception as exc:
        logger.error("DeepSeek unavailable: %s", exc)
        await respond(update, user_id, combined_text, TEXTS[lang]["ai_down"])
        return

    await send_reply(update, reply)
    await _log_message_async(update, combined_text, reply)


async def _log_message_async(update: Update, user_text: str, bot_reply: str) -> None:
    tg_user = update.effective_user
    try:
        await asyncio.to_thread(
            log_message,
            username=tg_user.username,
            full_name=tg_user.full_name,
            user_id=tg_user.id,
            user_msg=user_text,
            bot_reply=bot_reply,
        )
    except Exception as exc:
        logger.warning("Sheets error: %s", exc)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    user_id = update.effective_user.id
    user_text = update.message.text.strip()
    if not user_text:
        return

    pending_messages.setdefault(user_id, []).append(user_text)
    last_message_time[user_id] = monotonic()
    if user_id in pending_tasks and not pending_tasks[user_id].done():
        return

    async def watcher():
        try:
            while True:
                await asyncio.sleep(0.15)
                if monotonic() - last_message_time.get(user_id, 0) >= DEBOUNCE_SECONDS:
                    break
            messages = pending_messages.pop(user_id, [])
            last_message_time.pop(user_id, None)
            if messages:
                await process_user_message(user_id, " ".join(messages), update, context)
        finally:
            pending_tasks.pop(user_id, None)

    pending_tasks[user_id] = asyncio.create_task(watcher())


async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.contact:
        return
    user_id = update.effective_user.id
    session = get_session(user_id)
    lang = session.get("language", LANG_UZ)
    contact = update.message.contact
    if getattr(contact, "user_id", None) not in (None, user_id):
        await update.message.reply_text(TEXTS[lang]["phone_retry"])
        return
    raw_phone = contact.phone_number or ""
    phone = extract_phone(raw_phone)
    if not phone:
        await update.message.reply_text(TEXTS[lang]["phone_retry"])
        return
    await finalize_phone(update.effective_user, user_id, phone, context)
    record_exchange(user_id, "[Telegram contact shared]", TEXTS[lang]["phone_done"])
    await update.message.reply_text(TEXTS[lang]["phone_done"])


async def handle_unsupported(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    session = get_session(update.effective_user.id)
    await update.message.reply_text(TEXTS[session.get("language", LANG_UZ)]["unsupported"])


def _is_admin(update: Update) -> bool:
    user_id = update.effective_user.id if update.effective_user else 0
    chat_id = update.effective_chat.id if update.effective_chat else 0
    if ADMIN_USER_IDS and user_id in ADMIN_USER_IDS:
        return True
    if ADMIN_GROUP_CHAT_ID and str(chat_id) == ADMIN_GROUP_CHAT_ID:
        return True
    return False


async def admin_stats(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        return
    await update.message.reply_text(
        f"📊 Joriy statistika:\nFaol foydalanuvchilar: {len(histories)} ta\nSaqlangan telefonlar: {len(user_phones)} ta"
    )


async def chatid(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    title = chat.title or chat.full_name or "(private)"
    await update.message.reply_text(f"chat_id: {chat.id}\ntype: {chat.type}\ntitle: {title}")


def start_lead_api() -> None:
    if os.getenv("ENABLE_LEAD_API", "1").strip().lower() in {"0", "false", "no"}:
        logger.info("Website lead API is disabled")
        return
    host = os.getenv("LEAD_API_HOST", "127.0.0.1").strip() or "127.0.0.1"
    raw_port = os.getenv("PORT") or os.getenv("LEAD_API_PORT", "8000")
    port = int(raw_port)

    def run_server() -> None:
        import uvicorn
        from lead_api import app as api_app
        uvicorn.run(api_app, host=host, port=port, log_level="info")

    threading.Thread(target=run_server, name="satmakon-lead-api", daemon=True).start()
    logger.info("Website lead API: http://%s:%s/api/leads", host, port)


def main() -> None:
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN topilmadi")
    if ADMIN_GROUP_CHAT_ID:
        try:
            int(ADMIN_GROUP_CHAT_ID)
        except ValueError as exc:
            raise ValueError("ADMIN_GROUP_CHAT_ID noto'g'ri") from exc
    if not DEEPSEEK_API_KEY:
        logger.warning("DEEPSEEK_API_KEY is missing: deterministic FAQ and enrollment flow will still work")

    start_lead_api()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(CommandHandler("chatid", chatid))
    app.add_handler(MessageHandler(filters.CONTACT & filters.ChatType.PRIVATE, handle_contact))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_message))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_unsupported))

    logger.info("SATMakon Bot ishga tushdi%s...", " (DeepSeek + deterministic flow)" if client else " (deterministic fallback)")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()

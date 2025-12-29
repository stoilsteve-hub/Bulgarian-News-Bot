import os
import re
import html
import time
import sqlite3
import asyncio
import traceback
from io import BytesIO
from urllib.parse import quote_plus, urljoin, urlparse

import requests
import feedparser
from dotenv import load_dotenv
from openai import OpenAI

from telegram import Update, InputFile
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# ============================================================
# ENV
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, "config", ".env")
load_dotenv(ENV_PATH)

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
EDITOR_CHAT_ID = int((os.getenv("EDITOR_CHAT_ID") or "0").strip())
PUBLIC_CHANNEL_ID_RAW = (os.getenv("PUBLIC_CHANNEL_ID") or "@CtrlAltBG").strip()
if PUBLIC_CHANNEL_ID_RAW.startswith("@"):
    PUBLIC_CHANNEL_ID = PUBLIC_CHANNEL_ID_RAW
else:
    # If it's a numeric ID, convert to int. Handle -100 prefix if needed.
    try:
        # Most channels have -100 prefix. If user just gave the tail, add it.
        clean_id = str(PUBLIC_CHANNEL_ID_RAW).strip()
        if not clean_id.startswith("-"):
             if len(clean_id) > 5: # likely a channel tail
                 clean_id = "-100" + clean_id
        PUBLIC_CHANNEL_ID = int(clean_id)
    except ValueError:
        PUBLIC_CHANNEL_ID = PUBLIC_CHANNEL_ID_RAW
TELEGRAM_HANDLE = (os.getenv("TELEGRAM_HANDLE") or "@CtrlAltBG").strip()

JOB_TICK_SECONDS = int((os.getenv("JOB_TICK_SECONDS") or "360").strip())
RUN_COOLDOWN_SECONDS = int((os.getenv("RUN_COOLDOWN_SECONDS") or "300").strip())

PER_FEED_CAP = int((os.getenv("PER_FEED_CAP") or "10").strip())
MAX_PER_RUN = int((os.getenv("MAX_PER_RUN") or "1").strip())
MIN_SCORE = int((os.getenv("MIN_SCORE") or "1").strip())

OPENAI_MODEL = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
OPENAI_MAX_TOKENS = int((os.getenv("OPENAI_MAX_TOKENS") or "800").strip())
OPENAI_TEMPERATURE = float((os.getenv("OPENAI_TEMPERATURE") or "0.3").strip())

DB_PATH = os.path.join(BASE_DIR, "posted_items.sqlite")
DISABLE_PREVIEWS = True
AUTO_POST = (os.getenv("AUTO_POST", "false").lower().strip() == "true")

if not BOT_TOKEN or not OPENAI_API_KEY or not EDITOR_CHAT_ID or not PUBLIC_CHANNEL_ID:
    # We will let the user know if env is missing instead of raising immediately during implementation
    print(f"WARNING: Missing env vars in {ENV_PATH}")

# ============================================================
# SINGLE INSTANCE LOCK
# ============================================================

LOCK_PATH = os.path.join(BASE_DIR, ".bot.lock")

def acquire_lock_or_exit() -> None:
    if os.path.exists(LOCK_PATH):
        try:
            with open(LOCK_PATH, "r", encoding="utf-8") as f:
                pid_str = f.read().strip()
            if pid_str.isdigit():
                pid = int(pid_str)
                os.kill(pid, 0)
                raise SystemExit(f"[LOCK] Another bot instance is running (PID={pid}). Stop it first.")
        except ProcessLookupError:
            try:
                os.remove(LOCK_PATH)
            except Exception:
                pass
        except Exception:
            try:
                os.remove(LOCK_PATH)
            except Exception:
                pass

    with open(LOCK_PATH, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

def release_lock() -> None:
    try:
        if os.path.exists(LOCK_PATH):
            os.remove(LOCK_PATH)
    except Exception:
        pass

# ============================================================
# RSS
# ============================================================

feedparser.USER_AGENT = "BulgarianSensationalBot/1.0 (+https://t.me/CtrlAltBG)"

def google_news_rss(q: str) -> str:
    return f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=bg&gl=BG&ceid=BG:bg"

RSS_FEEDS = [
    ("Fakti.bg", "https://fakti.bg/feed"),
    ("BTA Bulgaria", "https://www.bta.bg/bg/rss/free"),
    ("BNT News", "https://bntnews.bg/bg/rss/news.xml"),
    ("Actualno Politics", "https://www.actualno.com/rss/politics"),
    ("24 Chasa", "https://www.24chasa.bg/rss"),
    ("Capital Bulgaria", "https://www.capital.bg/rss/?section=bulgaria"),
    ("Novini.bg via Google", google_news_rss("site:novini.bg")),
    ("News.bg via Google", google_news_rss("site:news.bg")),
    ("Vesti.bg via Google", google_news_rss("site:vesti.bg")),
    ("BTV Novinite via Google", google_news_rss("site:btvnovinite.bg")),
    ("Nova News via Google", google_news_rss("site:nova.bg")),
    ("Darik Regions via Google", google_news_rss("site:dariknews.bg/regioni")),
    ("Telegraph via Google", google_news_rss("site:telegraph.bg")),
    ("Standart via Google", google_news_rss("site:standartnews.com")),
]

KEYWORDS = [
    "Граждани за европейско развитие на България", "ГЕРБ", "Продължаваме промяната", "ПП", 
    "Демократична България", "ДБ", "ПП-ДБ", "Българска социалистическа партия", "БСП", 
    "Движение за права и свободи", "ДПС", "Има такъв народ", "ИТН", "Възраждане", 
    "Български възход", "Левицата", "Атака", "ВМРО", "НФСБ", "Да, България", "ДСБ", 
    "ЗНС", "ОЗ", "РЗБ", "КБ", "предсрочни избори", "парламентарни избори", "местни избори", 
    "президентски избори", "коалиционно правителство", "служебен кабинет", "оставка", 
    "вот на недоверие", "политическа криза", "нестабилност", "изборна умора", 
    "Делян Пеевски", "Бойко Борисов", "Кирил Петков", "Асен Василев", "Христо Иванов", 
    "Корнелия Нинова", "Слави Трифонов", "Костадин Костадинов", "Румен Радев", 
    "санкции Магнитски", "корупция", "антикорупция", "КПКОНПИ", "съдебна реформа", 
    "прокуратура", "главен прокурор", "ВСС", "олигархия", "задкулисие", "купуване на гласове", 
    "изборни измами", "масови протести", "гражданско недоволство", "Шенген", "сухопътен Шенген", 
    "мигрантски натиск", "нелегална миграция", "бежанци", "Европейски съюз", "ЕС", 
    "Европейска комисия", "еврофондове", "План за възстановяване и устойчивост", "ПВУ", 
    "еврозона", "въвеждане на еврото", "БНБ", "инфлация", "ръст на цените", "поскъпване", 
    "държавен бюджет", "бюджетен дефицит", "данъчни промени", "ДДС", "минимална работна заплата", 
    "пенсии", "социално напрежение", "енергийна криза", "високи цени на тока", "ВЕИ", 
    "Маришки басейн", "АЕЦ Козлодуй", "ядрена енергетика", "климатични промени", "наводнения", 
    "бедствено положение", "инфраструктурни щети", "катастрофи", "пътна безопасност", 
    "магистрали", "БДЖ", "транспортна криза", "икономическа несигурност", "инвестиции", 
    "ИТ сектор", "стартиращи компании", "недостиг на кадри", "пазар на труда", "стачки", 
    "синдикати", "образование", "реформа в образованието", "PISA", "дигитализация", 
    "електронно управление", "изкуствен интелект", "киберсигурност", "дезинформация", 
    "фалшиви новини", "медийна среда", "здравеопазване", "здравна реформа", "НЗОК", 
    "болници", "лекарства", "демографска криза", "емиграция", "раждаемост", "НАТО", 
    "войната в Украйна", "подкрепа за Украйна", "санкции срещу Русия", "руско влияние",
    # Added Priority Topics
    "протест", "митинг", "недоволство", "стачка", "бунт", "сблъсъци", "побой", "битка",
    "криза", "цени", "храна", "бензин", "горива", "сметки", "бедност", "оскъпяване",
    "училище", "университет", "образование", "студенти", "ученици", "преподаватели",
    "транспорт", "задръстване", "влак", "автобус", "пътна обстановка", "магистрала",
    "лична история", "трагедия", "съдба", "помощ", "дарение", "болест", "лечение",
    "арест", "полиция", "МВР", "акция", "разследване", "затвор", "престъпление",
    "корупция", "подкуп", "далавера", "злоупотреба", "кражба", "измама",
    # Global / Weather
    "Запад", "САЩ", "Тръмп", "Путин", "Русия", "Украйна",
    "буря", "ураган", "вятър", "пожар", "наводнение"
]

HOT_TERMS = [
    "скандал", "шокиращо", "ексклузивно", "арест", "взрив", "убийство", "бомба",
    "извънредно", "атака", "кризисен", "санкции", "заплаха", "конфликт", "стачка",
    "недостиг", "поскъпване", "бедствие", "трагедия", "катастрофа", "разкритие",
    "мафия", "задкулисие", "олигарх", "преврат", "разследване", "спешно",
    "протест", "цени", "криза", "бой", "училище", "университет", "болница", "пари",
    "храна", "ток", "парно", "вода", "гориво", "заплати", "пенсии", "бедност"
]

def normalize(text: str) -> str:
    # Remove punctuation and extra whitespace
    text = re.sub(r"[^\w\s]", " ", (text or "").lower())
    return re.sub(r"\s+", " ", text).strip()

def get_title_keywords(title: str) -> set[str]:
    # Extract unique words longer than 2 characters
    words = normalize(title).split()
    return {w for w in words if len(w) > 2}

def calc_similarity(title1: str, title2: str) -> float:
    set1 = get_title_keywords(title1)
    set2 = get_title_keywords(title2)
    if not set1 or not set2: return 0.0
    intersection = set1.intersection(set2)
    return len(intersection) / min(len(set1), len(set2))

def score_entry(title: str, summary: str) -> int:
    # Normalize title and summary separately
    t_norm = normalize(title)
    s_norm = normalize(summary)
    
    score = 0
    # Boost points for specific high-priority keywords
    # Title match = 8 pts, Summary match = 4 pts
    priority_boost = [
        "протест", "арест", "корупция", "трагедия", "катастрофа", "криза", "цени",
        "храна", "поскъпване", "бий", "бой", "полиция", "мвр", "болница"
    ]
    for pb in priority_boost:
        pb_lower = pb.lower()
        if pb_lower in t_norm:
            score += 8
        elif pb_lower in s_norm:
            score += 4

    # Standard keywords
    # Title match = 5 pts, Summary match = 2 pts
    for kw in KEYWORDS:
        kw_lower = kw.lower()
        if kw_lower in t_norm:
            score += 5
        elif kw_lower in s_norm:
            score += 2
            
    # Hot terms
    # Title match = 3 pts, Summary match = 1 pt
    for ht in HOT_TERMS:
        ht_lower = ht.lower()
        if ht_lower in t_norm:
            score += 3
        elif ht_lower in s_norm:
            score += 1
            
    return score

def detect_article_type(source_name: str, title: str, link: str) -> str:
    t = (source_name + " " + (title or "") + " " + (link or "")).lower()
    if any(x in t for x in ["коментар", "анализ", "мнение", "позиция", "opinion"]):
        return "analysis"
    return "news"

def fetch_feed(url: str) -> feedparser.FeedParserDict:
    resp = requests.get(url, timeout=20, headers={"User-Agent": feedparser.USER_AGENT})
    resp.raise_for_status()
    return feedparser.parse(resp.content)

def extract_item_id(entry) -> str:
    link = (entry.get("link") or "").strip()
    eid = (entry.get("id") or entry.get("guid") or link or "").strip()
    return eid

def strip_html_text(s: str) -> str:
    s = s or ""
    s = html.unescape(s)
    s = re.sub(r"<\s*br\s*/?\s*>", "\n", s, flags=re.I)
    s = re.sub(r"</p\s*>", "\n\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    s = re.sub(r"\s+", " ", s).strip()
    return s

# ============================================================
# PHOTO EXTRACTION
# ============================================================

META_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property|name)\s*=\s*["\'](?:og:image|twitter:image|twitter:image:src)["\'][^>]+content\s*=\s*["\']([^"\']+)["\']',
    re.I
)
META_OG_IMAGE_ALT_RE = re.compile(
    r'<meta[^>]+content\s*=\s*["\']([^"\']+)["\'][^>]+(?:property|name)\s*=\s*["\'](?:og:image|twitter:image|twitter:image:src)["\']',
    re.I
)
IMG_SRC_RE = re.compile(r"<img[^>]+src\s*=\s*['\"]([^'\"]+)['\"]", re.I)

def fetch_article_image(article_url: str) -> str:
    u = (article_url or "").strip()
    if not u:
        return ""
    try:
        print(f"[IMG] Fetching image from {u}...", flush=True)
        resp = requests.get(
            u,
            timeout=15,
            headers={
                "User-Agent": feedparser.USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            allow_redirects=True,
        )
        resp.raise_for_status()
        html_text = resp.text or ""

        m = META_OG_IMAGE_RE.search(html_text) or META_OG_IMAGE_ALT_RE.search(html_text)
        if m:
            img = (m.group(1) or "").strip()
            if img: 
                print(f"[IMG] Found OG/Twitter image: {img}", flush=True)
                return urljoin(u, img)

        # JSON-LD check (Capital.bg etc often use this)
        # Look for "image": "..." inside script tags
        json_ld_images = re.findall(r'["\']image["\']\s*:\s*["\']([^"\']+)["\']', html_text, re.I)
        if json_ld_images:
            for jimg in json_ld_images:
                if is_usable_image(jimg):
                    print(f"[IMG] Found JSON-LD image: {jimg}", flush=True)
                    return urljoin(u, jimg)

        m2 = IMG_SRC_RE.search(html_text)
        if m2:
            img = (m2.group(1) or "").strip()
            if img: 
                print(f"[IMG] Found fallback <img>: {img}", flush=True)
                return urljoin(u, img)
        print("[IMG] No usable image tags found.", flush=True)
    except Exception as e:
        print(f"[IMG] Error during image fetch: {e}", flush=True)
    return ""

def is_usable_image(image_url: str) -> bool:
    u = (image_url or "").strip()
    if not u: return False
    
    blocked = ["logo", "icon", "favicon", "placeholder", "sprite", "badge", "default"]
    u_low = u.lower()
    if any(k in u_low for k in blocked):
        return False
        
    # Skip too small images or SVGs
    if u_low.endswith(".svg"): return False
    
    return True

def download_image_bytes(image_url: str, max_bytes: int = 12_000_000) -> tuple[bytes, str]:
    r = requests.get(image_url, timeout=20, stream=True)
    r.raise_for_status()
    
    total = 0
    chunks = []
    for chunk in r.iter_content(chunk_size=64 * 1024):
        total += len(chunk)
        if total > max_bytes: raise ValueError("image too large")
        chunks.append(chunk)
        
    data = b"".join(chunks)
    ct = r.headers.get("Content-Type", "").lower()
    ext = ".jpg"
    if "png" in ct: ext = ".png"
    elif "webp" in ct: ext = ".webp"
    
    return data, f"photo{ext}"

# ============================================================
# DB
# ============================================================

def utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS drafts (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, text TEXT, status TEXT, error TEXT, image_url TEXT)")
    
    # Updated posted table with title_norm for deduplication
    c.execute("CREATE TABLE IF NOT EXISTS posted (item_id TEXT PRIMARY KEY, posted_at TEXT, title_norm TEXT)")
    
    # Simple migration: add title_norm if it doesn't exist
    try:
        c.execute("ALTER TABLE posted ADD COLUMN title_norm TEXT")
    except sqlite3.OperationalError:
        pass # column already exists
        
    c.execute("CREATE TABLE IF NOT EXISTS failures (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, source TEXT, item_id TEXT, stage TEXT, error TEXT)")
    conn.commit()
    return conn

def already_posted(conn: sqlite3.Connection, item_id: str) -> bool:
    c = conn.cursor()
    c.execute("SELECT 1 FROM posted WHERE item_id=?", (item_id,))
    return c.fetchone() is not None

def is_duplicate_story(conn: sqlite3.Connection, new_title: str, threshold: float = 0.6) -> bool:
    # Check for similar titles in the last 48 hours
    c = conn.cursor()
    # We use a date filter to keep it fast
    two_days_ago = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 172800))
    c.execute("SELECT title_norm FROM posted WHERE posted_at > ?", (two_days_ago,))
    rows = c.fetchall()
    
    for (old_title,) in rows:
        if not old_title: continue
        if calc_similarity(new_title, old_title) >= threshold:
            return True
    return False

def mark_posted(conn: sqlite3.Connection, item_id: str, title: str = "") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO posted (item_id, posted_at, title_norm) VALUES (?, ?, ?)", 
        (item_id, utc_now_iso(), normalize(title))
    )
    conn.commit()

def save_draft(conn: sqlite3.Connection, msg_html: str, status: str = "pending", image_url: str = "") -> int:
    cur = conn.cursor()
    cur.execute("INSERT INTO drafts (created_at, text, status, image_url) VALUES (?, ?, ?, ?)", (utc_now_iso(), msg_html, status, image_url))
    conn.commit()
    return int(cur.lastrowid)

# ============================================================
# TELEGRAM / FORMATTING
# ============================================================

def hard_clip(text: str, max_len: int = 3800) -> str:
    if len(text) <= max_len: return text
    return text[:max_len-20] + "\n...(truncated)"

async def publish_to_channel(bot, chat_id: int, text: str, image_url: str = "") -> None:
    image_url = (image_url or "").strip()
    text = hard_clip(text, 3900)
    
    # Telegram captions have a limit of 1024 characters.
    # If our text is short enough, we send the photo WITH the text as a caption.
    # This creates a single clean post without "Link Preview" junk.
    if image_url and len(text) < 1024:
        try:
            print(f"[PUB] Sending photo with caption to {chat_id}", flush=True)
            await bot.send_photo(
                chat_id=chat_id, 
                photo=image_url, 
                caption=text, 
                parse_mode=ParseMode.HTML
            )
            return # Done
        except Exception as e:
            print(f"[PUB] send_photo with caption failed: {e}. Falling back to separate messages.", flush=True)

    # Fallback/Default: Send photo and message separately
    photo_sent = False
    if image_url:
        try:
            await bot.send_photo(chat_id=chat_id, photo=image_url)
            photo_sent = True
        except Exception as e:
            try:
                data, fname = download_image_bytes(image_url)
                await bot.send_photo(chat_id=chat_id, photo=InputFile(BytesIO(data), filename=fname))
                photo_sent = True
            except: pass

    # For the text message, we ALWAYS disable the web page preview if we already have a photo
    # OR if the user wants it off. This removes that extra text you didn't like.
    await bot.send_message(
        chat_id=chat_id, 
        text=text, 
        parse_mode=ParseMode.HTML, 
        disable_web_page_preview=True # Keep it clean
    )

def build_message_html(headline: str, summary: str, details: str, source: str, link: str, hashtags: list[str]) -> str:
    h = html.escape(headline.strip())
    s = html.escape(summary.strip())
    d = html.escape(details.strip())
    src = html.escape(source.strip())
    l = html.escape(link)
    tags = " ".join(["#" + t.strip("#") for t in hashtags])
    return (
        f"<b>{h}</b>\n\n"
        f"{s}\n\n"
        f"<blockquote>{d}</blockquote>\n\n"
        f"📌 <b>Източник:</b> {src}\n"
        f"🔗 <a href='{l}'>Прочети повече</a>\n\n"
        f"{tags}\n"
        f"{TELEGRAM_HANDLE}"
    )

# ============================================================
# OPENAI
# ============================================================

def is_bulgarian_enough(text: str) -> bool:
    # Basic check for Cyrillic dominance
    letters = re.findall(r"[a-zA-Zа-яА-Я]", text)
    if not letters: return False
    cyr = sum(1 for c in letters if re.match(r"[а-яА-Я]", c))
    return (cyr / len(letters)) > 0.7

def extract_block(raw: str, label: str) -> str:
    m = re.search(rf"{label}:\s*\n?(.*?)(?=\n[A-Z]+:|\Z)", raw, flags=re.S | re.I)
    return m.group(1).strip() if m else ""

def generate_post(client: OpenAI, source: str, title: str, summary_raw: str, link: str, article_type: str) -> str:
    clean_summary = strip_html_text(summary_raw)
    
    prompt = f"""
Ти си журналист за популярния български Telegram канал "{TELEGRAM_HANDLE}". 
Твоята задача е да създадеш сензационно, но вярно обобщение на новина.

ИНСТРУКЦИИ:
- Пиши само на български език.
- Използвай емотикони за заглавието.
- Направи новината да звучи важно и интересно (сензационно).

РЕЛАВАНТНОСТ (КРИТИЧНО):
1. Новината трябва да се отнася директно за БЪЛГАРИЯ (събития, политици, институции, икономика в България).
2. АКО НЕ Е за България, тя трябва да бъде за: ВОЙНАТА, РУСИЯ, УКРАЙНА, ДОНАЛД ТРЪМП или ПУТИН.
3. Ако новината НЕ Е за България и НЕ Е за някоя от тези 5 специфични теми, изобщо не генерирай пост и върни само думата: SKIP.

Ако новината е релевантна, ВИНАГИ връщай EXACTLY 4 блока с етикети: HEADLINE, SUMMARY, DETAILS, HASHTAGS. (Без други обяснения).

HEADLINE: 1 изречение, закачливо заглавие с емоджи.
SUMMARY: 2-3 изречения, основната същност.
DETAILS: 5-8 кратки детайла (булети), разкриващи повече факти.
HASHTAGS: 4-6 релевантни хештага.

ИЗТОЧНИК: {source}
ЗАГЛАВИЕ: {title}
ОПИСАНИЕ: {clean_summary}
ЛИНК: {link}
ТИП: {article_type}
"""

    r = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "Ти си прецизен филтър и генератор на новини. Първо решаваш дали новината е за България, Русия, Украйна, Тръмп, Путин или Войната. Ако не е - връщаш SKIP. Ако е - генерираш пост в 4 блока."},
            {"role": "user", "content": prompt}
        ],
        temperature=OPENAI_TEMPERATURE,
        max_tokens=OPENAI_MAX_TOKENS
    )
    
    content = r.choices[0].message.content or ""
    
    if content.strip().upper() == "SKIP":
        print(f"[AI] Discarding irrelevant news (not Bulgaria/War/Russia/Ukraine/Trump).", flush=True)
        return "SKIP"

    if not is_bulgarian_enough(content):
        raise ValueError("AI output is not primarily Bulgarian.")

    h = extract_block(content, "HEADLINE")
    s = extract_block(content, "SUMMARY")
    d = extract_block(content, "DETAILS")
    tags_raw = extract_block(content, "HASHTAGS")
    tags = [t.strip("#, ") for t in tags_raw.split() if t.strip("#, ")]

    if not h or not s:
        # Fallback to a simpler extraction or re-generation if needed
        raise ValueError("Failed to extract HEADLINE or SUMMARY from AI response.")

    return build_message_html(h, s, d, source, link, tags)

# ============================================================
# BOT LOGIC
# ============================================================

async def run_rss_once(app: Application) -> None:
    bot = app.bot
    client: OpenAI = app.bot_data["openai_client"]
    conn: sqlite3.Connection = app.bot_data["db_conn"]
    
    print(f"\n[RSS] Starting scheduled scan at {time.strftime('%H:%M:%S')}...", flush=True)
    candidates = []
    for source, url in RSS_FEEDS:
        try:
            print(f"[RSS] Checking {source}...", flush=True)
            feed = fetch_feed(url)
            found_in_feed = 0
            for entry in (feed.entries or [])[:PER_FEED_CAP]:
                title = entry.get("title", "")
                summ = entry.get("summary", "") or entry.get("description", "")
                link = entry.get("link", "")
                item_id = extract_item_id(entry)
                
                if item_id and not already_posted(conn, item_id):
                    if is_duplicate_story(conn, title):
                        continue
                    
                    score = score_entry(title, summ)
                    if score >= MIN_SCORE:
                        candidates.append((score, source, title, summ, link, item_id))
                        found_in_feed += 1
            if found_in_feed > 0:
                print(f"[RSS]   --> {found_in_feed} new candidates found in {source}", flush=True)
        except Exception as e:
            print(f"[RSS]   [!] Error fetching {source}: {e}", flush=True)

    print(f"[RSS] Scan complete. Total candidates found: {len(candidates)}", flush=True)
    candidates.sort(key=lambda x: x[0], reverse=True)
    
    processed_count = 0
    for s, source, title, summ, link, item_id in candidates[:20]: # Check up to 20 candidates to find matches
        if processed_count >= MAX_PER_RUN:
            break

        try:
            print(f"[BOT] Processing candidate (Score: {s}): \"{title[:50]}...\" from {source}", flush=True)
            
            # AI Check & Generate
            msg_html = generate_post(client, source, title, summ, link, detect_article_type(source, title, link))
            
            if msg_html == "SKIP":
                mark_posted(conn, item_id, title)
                continue # Try next candidate in the same run

            # If we reached here, AI approved it
            image_url = fetch_article_image(link)
            if image_url and not is_usable_image(image_url):
                print(f"[BOT] Image discarded (filtered): {image_url}", flush=True)
                image_url = ""

            if AUTO_POST:
                print("[BOT] AUTO_POST enabled. Publishing to channel...", flush=True)
                await publish_to_channel(bot, PUBLIC_CHANNEL_ID, msg_html, image_url)
                save_draft(conn, msg_html, status="posted", image_url=image_url)
                print("[BOT] Successfully posted to channel.", flush=True)
            else:
                draft_id = save_draft(conn, msg_html, status="pending", image_url=image_url)
                print(f"[BOT] Draft #{draft_id} saved. Sending to editor chat...", flush=True)
                editor_msg = f"<b>Нова чернова #{draft_id}</b>\n\n{msg_html}\n\n/post {draft_id} | /skip {draft_id}"
                await bot.send_message(chat_id=EDITOR_CHAT_ID, text=editor_msg, parse_mode=ParseMode.HTML)
                print(f"[BOT] Notification sent to editor (ID: {EDITOR_CHAT_ID})", flush=True)
                
            mark_posted(conn, item_id, title)
            processed_count += 1
            
        except Exception as ex:
            print(f"[BOT] [!] Critical processing error: {ex}", flush=True)
            traceback.print_exc()

async def rss_job(context: ContextTypes.DEFAULT_TYPE):
    await run_rss_once(context.application)

async def cmd_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return
    did = context.args[0]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT text, image_url FROM drafts WHERE id=? AND status='pending'", (did,))
    row = c.fetchone()
    if row:
        await publish_to_channel(context.bot, PUBLIC_CHANNEL_ID, row[0], row[1])
        c.execute("UPDATE drafts SET status='posted' WHERE id=?", (did,))
        conn.commit()
        await update.message.reply_text(f"✅ Публикувано #{did}")
    conn.close()

async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return
    did = context.args[0]
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE drafts SET status='skipped' WHERE id=?", (did,))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"🗑 Прескочено #{did}")

async def post_init(app: Application):
    app.bot_data["openai_client"] = OpenAI(api_key=OPENAI_API_KEY)
    app.bot_data["db_conn"] = init_db()
    app.job_queue.run_repeating(rss_job, interval=JOB_TICK_SECONDS, first=5)
    await app.bot.send_message(chat_id=EDITOR_CHAT_ID, text="🤖 Ботът за български новини е стартиран!")

def main():
    print("--- [STARTUP] ---", flush=True)
    print(f"[STARTUP] Initializing Bulgarian News Bot...", flush=True)
    acquire_lock_or_exit()
    print("[STARTUP] Lock acquired.", flush=True)

    if not BOT_TOKEN:
        print("[STARTUP] [!] ERROR: BOT_TOKEN is missing in .env!", flush=True)
        return

    print(f"[STARTUP] Building application with token: {BOT_TOKEN[:8]}...", flush=True)
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    app.add_handler(CommandHandler("post", cmd_post))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CommandHandler("run", lambda u, c: run_rss_once(c.application)))
    
    print("[STARTUP] Bot is now polling for updates. Press Ctrl+C to stop.", flush=True)
    app.run_polling()

if __name__ == "__main__":
    main()

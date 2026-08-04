"""
DealsKoti Master Bot — Single File Version
Sab kuch yahan hai: storage, database, amazon API, aur sare commands.
Koi AI nahi. Koi external .py imports nahi.
"""

import os
import re
import io
import json
import html as html_lib
import logging
import urllib.parse
import aiohttp
import psycopg2
from contextlib import contextmanager
from datetime import datetime, timedelta
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    CallbackQueryHandler, filters, ContextTypes
)

logging.basicConfig(
    format="%(asctime)s — %(levelname)s — %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# =============================================================================
# ENV VARS
# =============================================================================
TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
except ValueError:
    ADMIN_ID = 0
    logger.error("ADMIN_ID env var must be a number!")

DATABASE_URL = os.getenv("DATABASE_URL")

CREDENTIAL_ID      = os.getenv("CREDENTIAL_ID", "")
CREDENTIAL_SECRET  = os.getenv("CREDENTIAL_SECRET", "")
CREDENTIAL_VERSION = os.getenv("CREDENTIAL_VERSION", "3.2")
MARKETPLACE        = os.getenv("MARKETPLACE", "www.amazon.in")

PARTNER_TAG = os.getenv("PARTNER_TAG", "")
if not PARTNER_TAG:
    logger.warning("PARTNER_TAG env var set nahi hai! Affiliate links mein tag nahi hoga.")

# =============================================================================
# CONSTANTS / PATTERNS
# =============================================================================
URL_REGEX = re.compile(r"(https?://[^\s\]\[<>\"']+)")

FOOTER_LINE_PATTERN = re.compile(
    r'^[-—\s]*(deal\s*from|buy\s*on|shop\s*on|source\s*:|via\s*:|'
    r'brought\s*by|available\s*on|check\s*on|grab\s*on|get\s*it\s*on|'
    r'amazon\s*deal|flipkart\s*deal|meesho\s*deal|deal\s*by|'
    r'posted\s*by|bot\s*by)\b.*$',
    re.IGNORECASE
)

ASIN_PAT = re.compile(
    r"/(?:dp|gp/product|exec/obidos/ASIN|o/ASIN)/([A-Za-z0-9]{10})",
    re.IGNORECASE
)

VERSION_TOKEN_URLS = {
    "2.1": "https://creatorsapi.auth.us-east-1.amazoncognito.com/oauth2/token",
    "2.2": "https://creatorsapi.auth.eu-south-2.amazoncognito.com/oauth2/token",
    "2.3": "https://creatorsapi.auth.us-west-2.amazoncognito.com/oauth2/token",
    "3.1": "https://api.amazon.com/auth/o2/token",
    "3.2": "https://api.amazon.co.uk/auth/o2/token",
    "3.3": "https://api.amazon.co.jp/auth/o2/token",
}

SCOPE    = "creatorsapi::default" if CREDENTIAL_VERSION.startswith("3.") else "creatorsapi/default"
API_BASE = "https://creatorsapi.amazon"
ITEMS_EP = f"{API_BASE}/catalog/v1/getItems"

PRODUCT_RESOURCES = [
    "images.primary.large",
    "images.primary.medium",
    "itemInfo.title",
    "offersV2.listings.price",
    "offersV2.listings.availability",
    "offersV2.listings.condition",
    "customerReviews.count",
    "customerReviews.starRating",
]

DUPLICATE_HOURS = 24

NOISE_WORDS = [
    'buy', 'shop', 'best price', 'order online', 'online', 'india',
    'get', 'deal', 'offer', 'discount', 'sale', 'free shipping',
    'lowest price', 'check price', 'view details', 'amazon', 'flipkart',
    'myntra', 'meesho', 'ajio', 'nykaa',
]

DEFAULT_CONFIG = {
    "groups": [],
    "buttons": {
        "btn1": {"label": "Join Channel", "url": "", "enabled": False},
        "btn2": {"label": "More Deals",   "url": "", "enabled": False},
    },
    "watermark": {"enabled": False, "text": ""},
}

_token_cache: dict = {"token": None, "expires_at": None}


# =============================================================================
# STORAGE (PostgreSQL config)
# =============================================================================
def _get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable set nahi hai!")
    return psycopg2.connect(DATABASE_URL)


@contextmanager
def get_db():
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate_button_keys(config: dict) -> tuple:
    buttons = config.get("buttons", {})
    changed = False
    for old, new in [("b1", "btn1"), ("b2", "btn2")]:
        if old in buttons:
            if new not in buttons:
                buttons[new] = buttons.pop(old)
            else:
                buttons.pop(old)
            changed = True
    config["buttons"] = buttons
    return config, changed


def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_config (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS seen_titles (
                    title_key TEXT      PRIMARY KEY,
                    posted_at TIMESTAMP NOT NULL
                )
            """)
    logger.info("Database tables ready.")
    try:
        cfg = load_config()
        cfg, changed = _migrate_button_keys(cfg)
        if changed:
            save_config(cfg)
            logger.info("Button keys migrated b1/b2 → btn1/btn2.")
    except Exception as e:
        logger.error(f"Migration error: {e}")


def load_config() -> dict:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM bot_config WHERE key = 'config'")
                row = cur.fetchone()
        if row:
            cfg = json.loads(row[0])
            cfg.setdefault("groups",    [])
            cfg.setdefault("buttons",   DEFAULT_CONFIG["buttons"].copy())
            cfg.setdefault("watermark", DEFAULT_CONFIG["watermark"].copy())
            return cfg
    except Exception as e:
        logger.error(f"Config load error: {e}")
    return {
        "groups": [],
        "buttons": {
            "btn1": {"label": "Join Channel", "url": "", "enabled": False},
            "btn2": {"label": "More Deals",   "url": "", "enabled": False},
        },
        "watermark": {"enabled": False, "text": ""},
    }


def save_config(config: dict):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO bot_config (key, value)
                    VALUES ('config', %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                    """,
                    (json.dumps(config, ensure_ascii=False),),
                )
    except Exception as e:
        logger.error(f"Config save error: {e}")


# =============================================================================
# DATABASE (duplicate detection)
# =============================================================================
def clean_title(title: str) -> str:
    if not title:
        return ""
    title = title.lower().strip()
    for word in NOISE_WORDS:
        title = re.sub(r'\b' + re.escape(word) + r'\b', ' ', title)
    title = re.sub(r'[^\w\s]', ' ', title)
    title = re.sub(r'\s+', ' ', title).strip()
    return title


def cleanup_old_entries():
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cutoff = datetime.now() - timedelta(hours=DUPLICATE_HOURS)
                cur.execute(
                    "DELETE FROM seen_titles WHERE posted_at < %s", (cutoff,)
                )
    except Exception as e:
        logger.error(f"Cleanup error: {e}")


def is_duplicate(title: str) -> tuple:
    if not title:
        return False, None
    cleaned = clean_title(title)
    if not cleaned:
        return False, None
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT posted_at FROM seen_titles WHERE title_key = %s",
                    (cleaned,),
                )
                row = cur.fetchone()
        if row:
            posted_at = row[0]
            diff = datetime.now() - posted_at
            if diff < timedelta(hours=DUPLICATE_HOURS):
                hours_ago = int(diff.total_seconds() / 3600)
                mins_ago  = int((diff.total_seconds() % 3600) / 60)
                if hours_ago == 0:
                    time_str = f"{mins_ago} minute pehle"
                else:
                    time_str = f"{hours_ago} ghante {mins_ago} min pehle"
                return True, time_str
    except Exception as e:
        logger.error(f"Duplicate check error: {e}")
    return False, None


def mark_posted(title: str):
    if not title:
        return
    cleaned = clean_title(title)
    if not cleaned:
        return
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO seen_titles (title_key, posted_at)
                    VALUES (%s, %s)
                    ON CONFLICT (title_key)
                    DO UPDATE SET posted_at = EXCLUDED.posted_at
                    """,
                    (cleaned, datetime.now()),
                )
    except Exception as e:
        logger.error(f"Mark posted error: {e}")


# =============================================================================
# AMAZON API
# =============================================================================
async def _get_token() -> str | None:
    now = datetime.now()
    if _token_cache["token"] and _token_cache["expires_at"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]

    if not CREDENTIAL_ID or not CREDENTIAL_SECRET:
        logger.error("CREDENTIAL_ID ya CREDENTIAL_SECRET set nahi hai")
        return None

    token_url = VERSION_TOKEN_URLS.get(CREDENTIAL_VERSION)
    if not token_url:
        logger.error(f"Unsupported CREDENTIAL_VERSION: {CREDENTIAL_VERSION}")
        return None

    is_lwa = CREDENTIAL_VERSION.startswith("3.")
    token_payload = {
        "grant_type":    "client_credentials",
        "client_id":     CREDENTIAL_ID,
        "client_secret": CREDENTIAL_SECRET,
        "scope":         SCOPE,
    }

    try:
        async with aiohttp.ClientSession() as session:
            if is_lwa:
                req = session.post(
                    token_url,
                    json=token_payload,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=15),
                )
            else:
                req = session.post(
                    token_url,
                    data=token_payload,
                    timeout=aiohttp.ClientTimeout(total=15),
                )
            async with req as resp:
                if resp.status == 200:
                    data       = await resp.json()
                    token      = data.get("access_token")
                    expires_in = data.get("expires_in", 3600)
                    _token_cache["token"]      = token
                    _token_cache["expires_at"] = now + timedelta(seconds=expires_in - 60)
                    logger.info("Amazon Creators API token mila!")
                    return token
                body = await resp.text()
                logger.error(f"Token error {resp.status}: {body[:300]}")
                return None
    except Exception as e:
        logger.error(f"Token fetch fail: {e}")
        return None


def extract_asin(url: str) -> str | None:
    url = url.strip()
    if re.fullmatch(r"[A-Za-z0-9]{10}", url):
        return url.upper()
    m = ASIN_PAT.search(url)
    if m:
        return m.group(1).upper()
    q = re.search(r"[?&]ASIN=([A-Za-z0-9]{10})", url, re.IGNORECASE)
    if q:
        return q.group(1).upper()
    return None


def is_amazon_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
        host   = parsed.netloc.lower()
        return bool(
            re.search(r'(^|\.)amazon\.(in|com|co\.uk|co\.jp|de|fr|it|es|ca|com\.au)$', host)
            or re.search(r'(^|\.)amzn\.(to|in)$', host)
            or re.search(r'^link\.amazon\.(com|in)$', host)
        )
    except Exception:
        return False


def is_amazon_search_url(url: str) -> bool:
    markers = ["/s?", "/s/", "field-keywords", "/b?", "node=", "/deals", "/gp/browse"]
    return any(m in url for m in markers)


def _strip_tag_param(url: str) -> str:
    try:
        parsed    = urllib.parse.urlparse(url)
        params    = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        params.pop("tag", None)
        new_query = urllib.parse.urlencode(params, doseq=True)
        return urllib.parse.urlunparse(parsed._replace(query=new_query))
    except Exception:
        return url


def make_affiliate_url(asin: str) -> str:
    base = f"https://{MARKETPLACE}/dp/{asin}"
    if PARTNER_TAG:
        return f"{base}?tag={PARTNER_TAG}"
    return base


async def _resolve_redirect(url: str) -> str:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                allow_redirects=True,
                max_redirects=5,
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"},
            ) as resp:
                return str(resp.url)
    except Exception:
        return url


async def get_short_affiliate_link(url: str) -> str:
    asin = extract_asin(url)
    if not asin:
        resolved = await _resolve_redirect(url)
        asin     = extract_asin(resolved)
    if asin:
        return make_affiliate_url(asin)
    cleaned = _strip_tag_param(url)
    if PARTNER_TAG:
        sep = "&" if "?" in cleaned else "?"
        return f"{cleaned}{sep}tag={PARTNER_TAG}"
    return cleaned


def _parse_item(item: dict) -> dict:
    result: dict = {}

    title_data      = item.get("itemInfo", {}).get("title", {})
    result["title"] = (title_data.get("displayValue", "") if title_data else "").strip()

    img_primary         = item.get("images", {}).get("primary", {})
    img                 = img_primary.get("large") or img_primary.get("medium") or img_primary.get("small") or {}
    result["image_url"] = img.get("url", "") if img else ""

    result["deal_price"]    = ""
    result["actual_price"]  = ""
    result["deal_amount"]   = 0.0
    result["actual_amount"] = 0.0
    result["discount_pct"]  = 0
    result["savings"]       = ""

    listings = item.get("offersV2", {}).get("listings", [])
    if listings:
        listing   = listings[0]
        price_obj = listing.get("price", {})
        money     = price_obj.get("money", {})
        if money:
            result["deal_price"]  = money.get("displayAmount", "")
            result["deal_amount"] = float(money.get("amount", 0) or 0)

        savings_obj = price_obj.get("savings", {})
        sav_money   = savings_obj.get("money", {})
        if sav_money:
            sav_amt          = float(sav_money.get("amount", 0) or 0)
            result["savings"] = sav_money.get("displayAmount", "")
            if sav_amt and result["deal_amount"]:
                mrp_amt                 = result["deal_amount"] + sav_amt
                result["actual_amount"] = mrp_amt
                result["actual_price"]  = f"₹{mrp_amt:,.0f}"

        pct = savings_obj.get("percentage")
        if pct is not None:
            result["discount_pct"] = int(pct)
        elif result["deal_amount"] and result["actual_amount"]:
            try:
                result["discount_pct"] = round(
                    (result["actual_amount"] - result["deal_amount"]) / result["actual_amount"] * 100
                )
            except Exception:
                pass

    cr                     = item.get("customerReviews", {})
    star                   = cr.get("starRating", {})
    result["rating"]       = str(star.get("value", "")).strip() if star else ""
    count                  = cr.get("count")
    result["review_count"] = f"{count:,}" if isinstance(count, int) else str(count or "")

    return result


async def get_product_by_asin(asin: str) -> dict | None:
    token = await _get_token()
    if not token:
        return None

    payload = {
        "partnerTag": PARTNER_TAG,
        "itemIds":    [asin],
        "resources":  PRODUCT_RESOURCES,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                ITEMS_EP,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "x-marketplace": MARKETPLACE,
                    "Content-Type":  "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 403:
                    _token_cache["token"]      = None
                    _token_cache["expires_at"] = None
                    logger.error("Amazon API 403 — token invalidated")
                    return None
                if resp.status not in (200, 206):
                    body = await resp.text()
                    logger.error(f"GetItems {resp.status}: {body[:300]}")
                    return None
                data  = await resp.json()
                items = data.get("itemsResult", {}).get("items", [])
                if items:
                    parsed                   = _parse_item(items[0])
                    parsed["asin"]           = asin
                    parsed["affiliate_link"] = make_affiliate_url(asin)
                    logger.info(f"Creators API product mila: {asin}")
                    return parsed
                errors = data.get("errors", [])
                msg    = errors[0].get("message", "") if errors else "Product not found"
                logger.warning(f"ASIN {asin} — {msg}")
                return None
    except Exception as e:
        logger.error(f"GetItems call fail: {e}")
        return None


async def enrich_amazon_url(url: str) -> dict | None:
    resolved = url
    if (
        "amzn.to" in url or "amzn.in" in url
        or "link.amazon.com" in url or "link.amazon.in" in url
    ):
        resolved = await _resolve_redirect(url)

    asin = extract_asin(resolved)
    if not asin:
        asin = extract_asin(url)
    if asin:
        return await get_product_by_asin(asin)
    logger.warning(f"ASIN nahi mila: {url[:80]}")
    return None


# =============================================================================
# WATERMARK HELPERS
# =============================================================================
async def _download_image(url: str) -> bytes | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": "Mozilla/5.0"},
            ) as resp:
                if resp.status == 200:
                    return await resp.read()
    except Exception as e:
        logger.error(f"Image download failed: {e}")
    return None


def _apply_watermark(image_bytes: bytes, text: str) -> bytes:
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        font_size = max(14, img.width // 28)
        try:
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size
            )
        except Exception:
            try:
                font = ImageFont.truetype("/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf", font_size)
            except Exception:
                font = ImageFont.load_default()

        draw  = ImageDraw.Draw(img)
        bbox  = draw.textbbox((0, 0), text, font=font)
        tw    = bbox[2] - bbox[0]
        th    = bbox[3] - bbox[1]
        pad   = 8
        mar   = 10
        x     = img.width  - tw - pad * 2 - mar
        y     = img.height - th - pad * 2 - mar

        overlay      = Image.new("RGBA", img.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.rectangle(
            [x, y, x + tw + pad * 2, y + th + pad * 2],
            fill=(0, 0, 0, 150),
        )
        img  = Image.alpha_composite(img, overlay)
        draw = ImageDraw.Draw(img)
        draw.text((x + pad, y + pad), text, font=font, fill=(255, 255, 255, 255))

        img_rgb = img.convert("RGB")
        buf = io.BytesIO()
        img_rgb.save(buf, format="JPEG", quality=90)
        return buf.getvalue()
    except Exception as e:
        logger.error(f"Watermark apply failed: {e}")
        return image_bytes


# =============================================================================
# HELPERS
# =============================================================================
def is_admin(uid):
    return ADMIN_ID != 0 and uid == ADMIN_ID


def extract_urls(text: str) -> list:
    return URL_REGEX.findall(text) if text else []


def get_amazon_urls(urls: list) -> list:
    return [u for u in urls if is_amazon_url(u)]


async def replace_amazon_links(text: str, urls: list) -> str:
    result = text
    for url in urls:
        if is_amazon_url(url):
            short = await get_short_affiliate_link(url)
            result = result.replace(url, short)
    return result


def _truncate_caption(text: str, max_len: int = 1020) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


# =============================================================================
# AMAZON CAPTION BUILDER
# =============================================================================
def build_amazon_caption(product: dict, short_link: str) -> str:
    title        = product.get("title", "")
    deal_price   = product.get("deal_price", "")
    actual_price = product.get("actual_price", "")
    discount     = product.get("discount_pct", 0)
    savings      = product.get("savings", "")
    rating       = product.get("rating", "")
    reviews      = product.get("review_count", "")

    lines = ["🙏Jai Shree Ram Dosto🙏\n"]

    if title:
        lines.append(f"<b>{html_lib.escape(title)}</b>\n")

    if deal_price:
        price_line = f"💰 <b>Price: {html_lib.escape(deal_price)}</b>"
        if actual_price and discount:
            price_line += f"  <s>{html_lib.escape(actual_price)}</s>  🔥 {discount}% OFF"
        elif actual_price:
            price_line += f"  <s>{html_lib.escape(actual_price)}</s>"
        lines.append(price_line)

    if savings:
        lines.append(f"💵 Bachao: <b>{html_lib.escape(savings)}</b>")

    if rating or reviews:
        star_line = ""
        if rating:
            star_line += f"⭐ {html_lib.escape(str(rating))}/5"
        if reviews:
            star_line += f"  ({html_lib.escape(str(reviews))} reviews)"
        if star_line:
            lines.append(star_line)

    lines.append(f'\n🔗 <b><a href="{html_lib.escape(short_link)}">{html_lib.escape(short_link)}</a></b>')
    return _truncate_caption("\n".join(lines), 1020)


# =============================================================================
# MARKUP BUILDER
# =============================================================================
def build_final_markup(config: dict):
    buttons_cfg = config.get("buttons", {})
    row = []
    for btn_key in ["btn1", "btn2"]:
        btn = buttons_cfg.get(btn_key, {})
        if btn.get("enabled") and btn.get("label") and btn.get("url"):
            row.append(InlineKeyboardButton(btn["label"], url=btn["url"]))
    return InlineKeyboardMarkup([row]) if row else None


# =============================================================================
# TEXT → HTML CONVERTER
# =============================================================================
def _py_to_utf16_len(text: str) -> int:
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def remove_footer(plain_text: str, entities: list):
    lines = plain_text.split('\n')
    while lines and not lines[-1].strip():
        lines.pop()
    changed = True
    while changed and lines:
        changed = False
        if FOOTER_LINE_PATTERN.match(lines[-1].strip()):
            lines.pop()
            changed = True
    cleaned = '\n'.join(lines).rstrip()
    cutoff_utf16 = _py_to_utf16_len(cleaned)
    filtered = [e for e in (entities or []) if e.offset + e.length <= cutoff_utf16]
    return cleaned, filtered


def _build_utf16_map(text: str) -> list:
    mapping = []
    for py_idx, ch in enumerate(text):
        mapping.append(py_idx)
        if ord(ch) > 0xFFFF:
            mapping.append(py_idx)
    mapping.append(len(text))
    return mapping


def entities_to_html(text: str, entities: list) -> str:
    if not entities:
        return html_lib.escape(text)

    utf16_map  = _build_utf16_map(text)
    open_tags  = [""] * len(text)
    close_tags = [""] * len(text)

    for ent in sorted(entities, key=lambda e: (e.offset, -e.length)):
        s_utf16 = ent.offset
        e_utf16 = ent.offset + ent.length
        s = utf16_map[s_utf16] if s_utf16 < len(utf16_map) else s_utf16
        e = utf16_map[e_utf16] if e_utf16 < len(utf16_map) else e_utf16
        if e > len(text):
            continue
        etype = ent.type

        if etype == "url":
            open_tags[s]    = '<b>' + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + '</b>'
        elif etype == "text_link":
            url = html_lib.escape(ent.url or "")
            open_tags[s]    = f'<a href="{url}"><b>' + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + '</b></a>'
        elif etype == "bold":
            open_tags[s]    = '<b>' + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + '</b>'
        elif etype == "italic":
            open_tags[s]    = '<i>' + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + '</i>'
        elif etype == "underline":
            open_tags[s]    = '<u>' + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + '</u>'
        elif etype == "strikethrough":
            open_tags[s]    = '<s>' + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + '</s>'
        elif etype == "code":
            open_tags[s]    = '<code>' + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + '</code>'
        elif etype == "pre":
            open_tags[s]    = '<pre>' + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + '</pre>'
        elif etype == "spoiler":
            open_tags[s]    = '<tg-spoiler>' + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + '</tg-spoiler>'

    result = []
    for i, ch in enumerate(text):
        result.append(open_tags[i])
        result.append(html_lib.escape(ch))
        result.append(close_tags[i])
    return ''.join(result)


# =============================================================================
# ADMIN REPLY HELPERS
# =============================================================================
def _channel_html_link(channel: str) -> str:
    ch = channel.strip()
    if ch.startswith("@"):
        name = ch[1:]
        return f'<a href="https://t.me/{name}">{ch}</a>'
    return f"<code>{ch}</code>"


async def _send_admin_reply(
    msg,
    headline: str,
    sent_channels: list = None,
    errors: list        = None,
    extra_line: str     = "",
):
    lines = [headline]

    if extra_line:
        lines.append(extra_line)

    if sent_channels:
        lines.append("")
        lines.append("📢 <b>Post kiya:</b>")
        for ch in sent_channels:
            lines.append(f"  • {_channel_html_link(ch)}")
    elif sent_channels is not None:
        lines.append("")
        lines.append(
            "⚠️ Koi channel nahi mila!\n"
            "/addgroup se channel set karo."
        )

    if errors:
        lines.append("\n❌ <b>Errors:</b>")
        for err in errors:
            lines.append(f"  • {html_lib.escape(str(err))}")

    await msg.reply_text(
        "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True
    )


# =============================================================================
# UI HELPERS
# =============================================================================
def _toggle_keyboard(groups):
    buttons = []
    for i, g in enumerate(groups):
        status = "✅ ON" if g.get("enabled", True) else "❌ OFF"
        buttons.append([InlineKeyboardButton(
            f"{status} — {g.get('name', 'Group')}",
            callback_data=f"toggle_{i}"
        )])
    buttons.append([InlineKeyboardButton("💾 Save & Done", callback_data="toggle_done")])
    return InlineKeyboardMarkup(buttons)


def _group_select_kb(prefix):
    config = load_config()
    buttons = []
    for i, g in enumerate(config.get("groups", [])):
        buttons.append([InlineKeyboardButton(
            g.get("name", f"Group {i+1}"),
            callback_data=f"{prefix}_{i}"
        )])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


def _channel_select_kb(group_idx, prefix):
    config = load_config()
    groups = config.get("groups", [])
    if group_idx >= len(groups):
        return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]])
    buttons = []
    for j, ch_obj in enumerate(groups[group_idx].get("channels", [])):
        ch = ch_obj.get("channel", f"Channel {j+1}")
        buttons.append([InlineKeyboardButton(ch, callback_data=f"{prefix}_{group_idx}_{j}")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


def _setbutton_status_text(buttons: dict) -> str:
    b1 = buttons.get("btn1", {})
    b2 = buttons.get("btn2", {})
    b1_status = "✅ ON" if b1.get("enabled") else "❌ OFF"
    b2_status = "✅ ON" if b2.get("enabled") else "❌ OFF"
    b1_label  = b1.get("label", "Button 1")
    b2_label  = b2.get("label", "Button 2")
    b1_url    = b1.get("url") or "—"
    b2_url    = b2.get("url") or "—"
    return (
        f"📌 <b>Button 1</b> — {b1_status}\n"
        f"   Naam: {html_lib.escape(b1_label)}\n"
        f"   Link: <code>{html_lib.escape(b1_url)}</code>\n\n"
        f"📌 <b>Button 2</b> — {b2_status}\n"
        f"   Naam: {html_lib.escape(b2_label)}\n"
        f"   Link: <code>{html_lib.escape(b2_url)}</code>"
    )


def _setbutton_main_kb(buttons: dict) -> InlineKeyboardMarkup:
    b1_label = buttons.get("btn1", {}).get("label", "Button 1")
    b2_label = buttons.get("btn2", {}).get("label", "Button 2")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✏️ {b1_label}", callback_data="sb_btn1")],
        [InlineKeyboardButton(f"✏️ {b2_label}", callback_data="sb_btn2")],
        [InlineKeyboardButton("❌ Cancel",       callback_data="cancel")],
    ])


def _setbutton_detail_kb(btn_key: str, btn: dict) -> InlineKeyboardMarkup:
    toggle_label = "🟢 Turn ON" if not btn.get("enabled") else "🔴 Turn OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Rename",   callback_data=f"sb_{btn_key}_rename")],
        [InlineKeyboardButton("🔗 Set Link", callback_data=f"sb_{btn_key}_link")],
        [InlineKeyboardButton(toggle_label,  callback_data=f"sb_{btn_key}_toggle")],
        [InlineKeyboardButton("⬅️ Back",     callback_data="sb_main")],
    ])


def _btn_detail_text(btn_key: str, btn: dict) -> str:
    status = "✅ ON" if btn.get("enabled") else "❌ OFF"
    label  = btn.get("label", "—")
    url    = btn.get("url") or "—"
    return (
        f"🎛️ <b>Button {btn_key[-1]} Settings</b>\n\n"
        f"Status: {status}\n"
        f"Naam: <b>{html_lib.escape(label)}</b>\n"
        f"🔗 Link: <code>{html_lib.escape(url)}</code>\n\n"
        f"<i>Kaun sa change karna hai?</i>"
    )


def _watermark_status_text(wm: dict) -> str:
    status = "✅ ON" if wm.get("enabled") else "❌ OFF"
    text   = wm.get("text", "") or "—"
    return (
        f"🖼️ <b>Watermark Settings</b>\n\n"
        f"Status: {status}\n"
        f"Text: <b>{html_lib.escape(text)}</b>"
    )


def _watermark_kb(wm: dict) -> InlineKeyboardMarkup:
    toggle_label = "🔴 Turn OFF" if wm.get("enabled") else "🟢 Turn ON"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_label,   callback_data="wm_toggle")],
        [InlineKeyboardButton("✏️ Set Text",  callback_data="wm_set_text")],
        [InlineKeyboardButton("❌ Cancel",    callback_data="cancel")],
    ])


# =============================================================================
# COMMANDS
# =============================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "🤖 <b>DealsKoti Master Bot</b>\n\n"
        "Admin deal message bhejo — bot automatically sahi channels pe post karega.\n\n"
        "/help — sare commands dekho",
        parse_mode="HTML"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "📖 <b>DealsKoti Bot — Sare Commands</b>\n\n"
        "📊 /status — Groups, channels aur settings dekho\n"
        "🔁 /manage — Groups ON/OFF karo\n"
        "✏️ /editgroup — Group ke inline buttons toggle karo\n"
        "✏️ /rename — Group naam badlo\n"
        "🎛️ /setbutton — Post ke neeche buttons set karo\n"
        "🖼️ /setwatermark — Image watermark settings\n"
        "➕ /addgroup — Naya group banao\n"
        "➕ /addchannel — Group mein channel add karo\n"
        "🗑️ /deletegroup — Group delete karo\n"
        "🗑️ /deletechannel — Channel hatao\n"
        "🧪 /testamz — Amazon API test karo\n"
        "💾 /exportconfig — Config backup karo\n"
        "ℹ️ /start — Bot ki info",
        parse_mode="HTML"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    config  = load_config()
    groups  = config.get("groups", [])
    buttons = config.get("buttons", {})
    wm      = config.get("watermark", {})

    b1        = buttons.get("btn1", {})
    b2        = buttons.get("btn2", {})
    wm_status = "✅ ON" if wm.get("enabled") else "❌ OFF"
    wm_text   = wm.get("text", "") or "—"

    lines = [
        "⚙️ <b>Bot Status</b>\n",
        "🎛️ <b>Buttons:</b>",
        f"  Button 1: {'✅ ON' if b1.get('enabled') else '❌ OFF'} — <b>{html_lib.escape(b1.get('label', '-'))}</b>",
        f"  Button 2: {'✅ ON' if b2.get('enabled') else '❌ OFF'} — <b>{html_lib.escape(b2.get('label', '-'))}</b>",
        "",
        "🖼️ <b>Watermark:</b>",
        f"  Status: {wm_status}  |  Text: <b>{html_lib.escape(wm_text)}</b>",
        "",
    ]

    if not groups:
        lines.append("⚠️ Koi group nahi — /addgroup se banao")
    else:
        lines.append("📢 <b>Groups:</b>")
        for i, g in enumerate(groups, 1):
            st = "✅ ON" if g.get("enabled", True) else "❌ OFF"
            lines.append(f"\n<b>{i}. {html_lib.escape(g.get('name', 'Group'))}</b> — {st}")
            for ch_obj in g.get("channels", []):
                lines.append(f"   📢 {html_lib.escape(ch_obj.get('channel', ''))}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_manage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    config = load_config()
    groups = config.get("groups", [])
    if not groups:
        await update.message.reply_text(
            "⚠️ Koi group nahi hai. /addgroup se pehle group banao.",
            parse_mode="HTML"
        )
        return
    await update.message.reply_text(
        "🔁 <b>Groups ON/OFF karo:</b>",
        reply_markup=_toggle_keyboard(groups),
        parse_mode="HTML"
    )


async def cmd_editgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    config = load_config()
    if not config.get("groups"):
        await update.message.reply_text("⚠️ Koi group nahi hai.")
        return
    await update.message.reply_text(
        "✏️ <b>Kaun sa group edit karna hai?</b>",
        reply_markup=_group_select_kb("eg_group"),
        parse_mode="HTML"
    )


async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    config = load_config()
    if not config.get("groups"):
        await update.message.reply_text("⚠️ Koi group nahi hai.")
        return
    await update.message.reply_text(
        "✏️ <b>Kaun sa group rename karna hai?</b>",
        reply_markup=_group_select_kb("ren_group"),
        parse_mode="HTML"
    )


async def cmd_setbutton(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    config  = load_config()
    buttons = config.get("buttons", {})
    await update.message.reply_text(
        "🎛️ <b>Button Settings</b>\n\n"
        + _setbutton_status_text(buttons)
        + "\n\n<i>Kaun sa configure karna hai?</i>",
        parse_mode="HTML",
        reply_markup=_setbutton_main_kb(buttons)
    )


async def cmd_setwatermark(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    config = load_config()
    wm     = config.get("watermark", {})
    await update.message.reply_text(
        _watermark_status_text(wm),
        parse_mode="HTML",
        reply_markup=_watermark_kb(wm)
    )


async def cmd_addgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    context.user_data["action"] = "wait_group_name"
    await update.message.reply_text(
        "➕ <b>Naya Group Banao</b>\n\nGroup ka naam type karo:",
        parse_mode="HTML"
    )


async def cmd_addchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    config = load_config()
    if not config.get("groups"):
        await update.message.reply_text(
            "⚠️ Pehle /addgroup se ek group banao.",
            parse_mode="HTML"
        )
        return
    await update.message.reply_text(
        "➕ <b>Channel Add Karo — Kaun se group mein?</b>",
        reply_markup=_group_select_kb("ac_group"),
        parse_mode="HTML"
    )


async def cmd_deletegroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    config = load_config()
    if not config.get("groups"):
        await update.message.reply_text("⚠️ Koi group nahi hai.")
        return
    await update.message.reply_text(
        "🗑️ <b>Kaun sa group delete karna hai?</b>",
        reply_markup=_group_select_kb("del_group"),
        parse_mode="HTML"
    )


async def cmd_deletechannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    config = load_config()
    if not config.get("groups"):
        await update.message.reply_text("⚠️ Koi group nahi hai.")
        return
    await update.message.reply_text(
        "🗑️ <b>Kaun se group se channel hatana hai?</b>",
        reply_markup=_group_select_kb("dc_group"),
        parse_mode="HTML"
    )


async def cmd_testamz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("🔄 Amazon Creators API test ho rahi hai...")
    try:
        test_asin = "B08N5WRWNW"
        product   = await get_product_by_asin(test_asin)
        short     = make_affiliate_url(test_asin)
        if product and product.get("title"):
            await update.message.reply_text(
                f"✅ <b>Amazon Creators API kaam kar raha hai!</b>\n\n"
                f"🏷️ Title: <code>{product['title'][:80]}</code>\n"
                f"💰 Deal Price: <b>{product.get('deal_price', 'N/A')}</b>\n"
                f"📉 Discount: <b>{product.get('discount_pct', 0)}%</b>\n"
                f"⭐ Rating: <b>{product.get('rating', 'N/A')}</b>\n"
                f"👥 Reviews: <b>{product.get('review_count', 'N/A')}</b>\n"
                f"🖼️ Image: <b>{'Mili ✅' if product.get('image_url') else 'Nahi mili ❌'}</b>\n"
                f"🔗 Affiliate link: <code>{short}</code>",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                "⚠️ Amazon API se product data nahi mila.\n"
                "CREDENTIAL_ID aur CREDENTIAL_SECRET check karo.",
                parse_mode="HTML"
            )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Amazon API error:\n<code>{html_lib.escape(str(e))}</code>",
            parse_mode="HTML"
        )


async def cmd_exportconfig(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    config      = load_config()
    config_json = json.dumps(config, indent=2, ensure_ascii=False)
    await update.message.reply_text(
        f"📦 <b>Config Export (Backup)</b>\n\n"
        f"Is JSON ko copy karke safe jagah save karo.\n\n"
        f"<pre>{html_lib.escape(config_json)}</pre>",
        parse_mode="HTML"
    )


# =============================================================================
# CHANNEL POSTER
# =============================================================================
async def _post_to_channels(context, config, send_fn, base_markup=None) -> tuple:
    sent_channels = []
    errors        = []
    for group in config.get("groups", []):
        if not group.get("enabled", True):
            continue
        show_buttons = group.get("show_buttons", True)
        group_markup = base_markup if show_buttons else None
        for ch_obj in group.get("channels", []):
            channel = ch_obj.get("channel", "").strip()
            if not channel:
                continue
            try:
                await send_fn(channel, group_markup)
                sent_channels.append(channel)
            except Exception as e:
                errors.append(f"{channel}: {e}")
    return sent_channels, errors


# =============================================================================
# MAIN DEAL HANDLER
# =============================================================================
async def handle_deal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return

    action = context.user_data.get("action")
    if action:
        await handle_text_input(update, context)
        return

    msg = update.message

    if msg.caption is not None:
        raw_plain    = msg.caption or ""
        raw_entities = list(msg.caption_entities or [])
        has_photo    = True
    elif msg.text:
        raw_plain    = msg.text or ""
        raw_entities = list(msg.entities or [])
        has_photo    = False
    else:
        raw_plain    = ""
        raw_entities = []
        has_photo    = bool(msg.photo or msg.document or msg.video)

    all_urls      = extract_urls(raw_plain)
    amazon_urls   = get_amazon_urls(all_urls)
    has_amazon    = len(amazon_urls) > 0
    single_amazon = len(amazon_urls) == 1

    if not raw_plain.strip() and not all_urls and not has_photo:
        await msg.reply_text("⚠️ Message mein koi text ya link nahi mila.")
        return

    if not raw_plain.strip() and not all_urls and has_photo:
        await msg.reply_text("⚠️ Photo ke saath product ka naam ya link bhi bhejo.")
        return

    config       = load_config()
    final_markup = build_final_markup(config)
    wm_config    = config.get("watermark", {})
    wm_enabled   = wm_config.get("enabled", False) and bool(wm_config.get("text", "").strip())
    wm_text      = wm_config.get("text", "").strip()

    try:
        cleanup_old_entries()
    except Exception:
        pass

    # ==========================================================================
    # CASE 1: Single Amazon product link
    # ==========================================================================
    if single_amazon and has_amazon:
        amazon_url = amazon_urls[0]

        resolved_url = amazon_url
        if (
            "amzn.to" in amazon_url or "amzn.in" in amazon_url
            or "link.amazon.com" in amazon_url or "link.amazon.in" in amazon_url
        ):
            resolved_url = await _resolve_redirect(amazon_url)

        if is_amazon_search_url(resolved_url):
            await msg.reply_text(
                "❌ <b>Yeh Amazon search page ka link hai — post nahi kiya.</b>\n\n"
                "Kisi specific product ka link bhejo 😊",
                parse_mode="HTML"
            )
            return

        wait_msg = await msg.reply_text("⏳ Amazon product data fetch ho raha hai...")

        product    = await enrich_amazon_url(amazon_url)
        short_link = await get_short_affiliate_link(amazon_url)

        await wait_msg.delete()

        if product and product.get("title"):
            title        = product["title"]
            dup, dup_time = is_duplicate(title)
            if dup:
                await msg.reply_text(
                    f"⚠️ <b>Yeh deal {dup_time} already post ho chuki hai — skip kiya.</b>\n\n"
                    f"🏷️ {html_lib.escape(title[:80])}",
                    parse_mode="HTML"
                )
                return
            caption_html = build_amazon_caption(product, short_link)
            image_url    = product.get("image_url", "")
            api_note     = ""
        else:
            title        = None
            image_url    = ""
            api_note     = "⚠️ Amazon API se data nahi mila — sirf affiliate link ke saath post kiya."
            cleaned_plain, cleaned_entities = remove_footer(raw_plain, raw_entities)
            body_html    = entities_to_html(cleaned_plain, cleaned_entities)
            escaped_url  = html_lib.escape(amazon_url)
            body_html    = body_html.replace(
                escaped_url,
                f'<b><a href="{html_lib.escape(short_link)}">{html_lib.escape(short_link)}</a></b>'
            )
            caption_html = _truncate_caption("🙏Jai Shree Ram Dosto🙏\n\n" + body_html, 1020)

        image_bytes = None
        if image_url and wm_enabled:
            raw_bytes = await _download_image(image_url)
            if raw_bytes:
                image_bytes = _apply_watermark(raw_bytes, wm_text)

        async def send_amazon(channel, markup):
            if image_bytes:
                await context.bot.send_photo(
                    chat_id=channel,
                    photo=image_bytes,
                    caption=caption_html,
                    parse_mode="HTML",
                    reply_markup=markup,
                )
            elif image_url:
                await context.bot.send_photo(
                    chat_id=channel,
                    photo=image_url,
                    caption=caption_html,
                    parse_mode="HTML",
                    reply_markup=markup,
                )
            else:
                await context.bot.send_message(
                    chat_id=channel,
                    text=caption_html,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=markup,
                )

        sent_channels, errors = await _post_to_channels(
            context, config, send_amazon, base_markup=final_markup
        )

        if sent_channels and title:
            mark_posted(title)

        await _send_admin_reply(
            msg,
            headline      = "✅ <b>Amazon Deal Post Ho Gaya!</b>" if sent_channels else "⚠️ <b>Koi channel nahi mila!</b>",
            sent_channels = sent_channels,
            errors        = errors,
            extra_line    = api_note,
        )
        return

    # ==========================================================================
    # CASE 2: Multiple Amazon links / Non-Amazon
    # ==========================================================================
    cleaned_plain, cleaned_entities = remove_footer(raw_plain, raw_entities)

    if has_amazon:
        updated_plain = await replace_amazon_links(cleaned_plain, amazon_urls)
    else:
        updated_plain = cleaned_plain

    dup_key = raw_plain.strip()[:300]
    if dup_key:
        dup, dup_time = is_duplicate(dup_key)
        if dup:
            await msg.reply_text(
                f"⚠️ <b>Yeh message {dup_time} already post ho chuka hai — skip kiya.</b>",
                parse_mode="HTML"
            )
            return

    GREETING   = "🙏Jai Shree Ram Dosto🙏\n\n"
    body_html  = entities_to_html(updated_plain, cleaned_entities)
    final_html = GREETING + body_html

    photo_bytes_wm = None
    if not has_amazon and has_photo and msg.photo and wm_enabled:
        try:
            photo_file     = await context.bot.get_file(msg.photo[-1].file_id)
            raw_photo      = bytes(await photo_file.download_as_bytearray())
            photo_bytes_wm = _apply_watermark(raw_photo, wm_text)
        except Exception as e:
            logger.error(f"Photo watermark error: {e}")

    async def send_normal(channel, markup):
        if photo_bytes_wm:
            await context.bot.send_photo(
                chat_id=channel,
                photo=photo_bytes_wm,
                caption=final_html,
                parse_mode="HTML",
                reply_markup=markup,
            )
        elif has_amazon:
            await context.bot.send_message(
                chat_id=channel,
                text=final_html,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=markup,
            )
        elif msg.caption is not None:
            await context.bot.copy_message(
                chat_id=channel,
                from_chat_id=msg.chat_id,
                message_id=msg.message_id,
                caption=final_html,
                parse_mode="HTML",
                reply_markup=markup,
            )
        elif msg.text:
            await context.bot.send_message(
                chat_id=channel,
                text=final_html,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=markup,
            )
        else:
            await context.bot.copy_message(
                chat_id=channel,
                from_chat_id=msg.chat_id,
                message_id=msg.message_id,
                reply_markup=markup,
            )

    sent_channels, errors = await _post_to_channels(
        context, config, send_normal, base_markup=final_markup
    )

    if sent_channels and dup_key:
        mark_posted(dup_key)

    await _send_admin_reply(
        msg,
        headline      = "✅ <b>Post Ho Gaya!</b>" if sent_channels else "⚠️ <b>Koi channel nahi mila!</b>",
        sent_channels = sent_channels,
        errors        = errors,
    )


# =============================================================================
# TEXT INPUT HANDLER (for multi-step commands)
# =============================================================================
async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    action = context.user_data.get("action", "")
    text   = (update.message.text or "").strip()
    config = load_config()
    groups = config.get("groups", [])

    if not text:
        return

    # --- Add group ---
    if action == "wait_group_name":
        context.user_data.pop("action", None)
        new_group = {
            "name":         text,
            "enabled":      True,
            "show_buttons": True,
            "channels":     [],
        }
        config["groups"].append(new_group)
        save_config(config)
        await update.message.reply_text(
            f"✅ Group <b>'{html_lib.escape(text)}'</b> bana diya!\n\n"
            f"Ab /addchannel se channels add karo.",
            parse_mode="HTML"
        )
        return

    # --- Add channel ---
    if action == "wait_channel_name":
        gi = context.user_data.pop("add_channel_group_idx", None)
        context.user_data.pop("action", None)
        if gi is None or gi >= len(groups):
            await update.message.reply_text("❌ Group nahi mila.")
            return
        ch_entry = {"channel": text, "categories": []}
        config["groups"][gi]["channels"].append(ch_entry)
        save_config(config)
        await update.message.reply_text(
            f"✅ Channel <b>{html_lib.escape(text)}</b> add ho gaya "
            f"→ group <b>{html_lib.escape(groups[gi].get('name',''))}</b>",
            parse_mode="HTML"
        )
        return

    # --- Watermark text ---
    if action == "wait_wm_text":
        context.user_data.pop("action", None)
        wm_text_new = text[:30]
        wm          = config.setdefault("watermark", {})
        wm["text"]  = wm_text_new
        config["watermark"] = wm
        save_config(config)
        await update.message.reply_text(
            f"✅ Watermark text set: <b>{html_lib.escape(wm_text_new)}</b>",
            parse_mode="HTML"
        )
        return

    # --- Button rename ---
    if action.endswith("_wait_label"):
        btn_key = context.user_data.get("sb_btn_key")
        context.user_data.pop("action", None)
        context.user_data.pop("sb_btn_key", None)
        if not btn_key:
            return
        label = text[:20]
        buttons = config.setdefault("buttons", {})
        btn     = buttons.setdefault(btn_key, {})
        btn["label"]    = label
        buttons[btn_key] = btn
        config["buttons"] = buttons
        save_config(config)
        await update.message.reply_text(
            f"✅ Button {btn_key[-1]} naam set: <b>{html_lib.escape(label)}</b>",
            parse_mode="HTML"
        )
        return

    # --- Button link ---
    if action.endswith("_wait_link"):
        btn_key = context.user_data.get("sb_btn_key")
        context.user_data.pop("action", None)
        context.user_data.pop("sb_btn_key", None)
        if not btn_key:
            return
        if not (text.startswith("https://") or text.startswith("http://") or text.startswith("t.me/")):
            await update.message.reply_text(
                "❌ Invalid URL. https:// ya t.me/ se shuru hona chahiye."
            )
            return
        buttons = config.setdefault("buttons", {})
        btn     = buttons.setdefault(btn_key, {})
        btn["url"]       = text
        buttons[btn_key] = btn
        config["buttons"] = buttons
        save_config(config)
        url_display = text[:50]
        await update.message.reply_text(
            f"✅ Button {btn_key[-1]} link set:\n<code>{html_lib.escape(url_display)}</code>",
            parse_mode="HTML"
        )
        return

    # --- Rename group ---
    if action == "wait_rename_text":
        gi       = context.user_data.pop("rename_group_idx", None)
        old_name = context.user_data.pop("rename_old_name", "")
        context.user_data.pop("action", None)
        if gi is None or gi >= len(groups):
            await update.message.reply_text("❌ Group nahi mila.")
            return
        config["groups"][gi]["name"] = text
        save_config(config)
        await update.message.reply_text(
            f"✅ Group rename ho gaya:\n"
            f"<b>{html_lib.escape(old_name)}</b> → <b>{html_lib.escape(text)}</b>",
            parse_mode="HTML"
        )
        return


# =============================================================================
# CALLBACK QUERY HANDLER
# =============================================================================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    data   = query.data
    config = load_config()
    groups = config.get("groups", [])

    await query.answer()

    if data == "cancel":
        context.user_data.clear()
        try:
            await query.edit_message_text("❌ Cancel ho gaya.")
        except Exception:
            pass
        return

    # --- Group toggle ---
    if data.startswith("toggle_"):
        if data == "toggle_done":
            try:
                await query.edit_message_text("✅ Groups save ho gaye!")
            except Exception:
                pass
            return
        try:
            idx = int(data.split("_")[1])
            config["groups"][idx]["enabled"] = not config["groups"][idx].get("enabled", True)
            save_config(config)
            await query.edit_message_reply_markup(reply_markup=_toggle_keyboard(config["groups"]))
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
        return

    # --- Edit group (show_buttons toggle) ---
    if data.startswith("eg_group_"):
        try:
            gi = int(data.split("_")[2])
            cur = config["groups"][gi].get("show_buttons", True)
            config["groups"][gi]["show_buttons"] = not cur
            save_config(config)
            status = "✅ ON" if config["groups"][gi]["show_buttons"] else "❌ OFF"
            await query.edit_message_text(
                f"Group <b>{html_lib.escape(config['groups'][gi].get('name',''))}</b>\n"
                f"Buttons: {status}",
                parse_mode="HTML"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
        return

    # --- Rename group ---
    if data.startswith("ren_group_"):
        try:
            gi       = int(data.split("_")[2])
            old_name = groups[gi].get("name", "")
            context.user_data["action"]           = "wait_rename_text"
            context.user_data["rename_group_idx"] = gi
            context.user_data["rename_old_name"]  = old_name
            await query.edit_message_text(
                f"✏️ <b>'{html_lib.escape(old_name)}'</b> ka naya naam type karo:",
                parse_mode="HTML"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
        return

    # --- Add channel to group ---
    if data.startswith("ac_group_"):
        try:
            gi = int(data.split("_")[2])
            context.user_data["add_channel_group_idx"] = gi
            context.user_data["action"] = "wait_channel_name"
            await query.edit_message_text(
                f"➕ <b>{html_lib.escape(groups[gi].get('name',''))}</b> mein channel add karo\n\n"
                f"Channel ID type karo (jaise @mychannel ya -100123456):",
                parse_mode="HTML"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
        return

    # --- Delete group ---
    if data.startswith("del_group_"):
        try:
            gi      = int(data.split("_")[2])
            removed = config["groups"].pop(gi)
            save_config(config)
            await query.edit_message_text(
                f"🗑️ Group <b>'{html_lib.escape(removed.get('name',''))}'</b> delete ho gaya.",
                parse_mode="HTML"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
        return

    # --- Delete channel (select group) ---
    if data.startswith("dc_group_"):
        try:
            gi = int(data.split("_")[2])
            context.user_data["dc_group_idx"] = gi
            await query.edit_message_text(
                f"🗑️ <b>{html_lib.escape(groups[gi].get('name',''))}</b> — Kaun sa channel delete karo?",
                reply_markup=_channel_select_kb(gi, "dc_ch"),
                parse_mode="HTML"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
        return

    # --- Delete channel (confirm) ---
    if data.startswith("dc_ch_"):
        try:
            parts   = data.split("_")
            gi, ci  = int(parts[2]), int(parts[3])
            removed = config["groups"][gi]["channels"].pop(ci)
            save_config(config)
            await query.edit_message_text(
                f"🗑️ Channel <b>{html_lib.escape(removed.get('channel',''))}</b> delete ho gaya.",
                parse_mode="HTML"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
        return

    # --- Watermark toggle ---
    if data == "wm_toggle":
        try:
            wm              = config.setdefault("watermark", {})
            wm["enabled"]   = not wm.get("enabled", False)
            config["watermark"] = wm
            save_config(config)
            await query.edit_message_text(
                _watermark_status_text(wm),
                parse_mode="HTML",
                reply_markup=_watermark_kb(wm)
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
        return

    # --- Watermark set text ---
    if data == "wm_set_text":
        context.user_data["action"] = "wait_wm_text"
        try:
            await query.edit_message_text(
                "✏️ <b>Watermark Text Daalo</b>\n\n"
                "Channel naam ya koi bhi text type karo (max 30 characters):\n"
                "<i>Example: @YourChannel</i>",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    # --- /setbutton main ---
    if data == "sb_main":
        cfg     = load_config()
        buttons = cfg.get("buttons", {})
        try:
            await query.edit_message_text(
                "🎛️ <b>Button Settings</b>\n\n"
                + _setbutton_status_text(buttons)
                + "\n\n<i>Kaun sa configure karna hai?</i>",
                parse_mode="HTML",
                reply_markup=_setbutton_main_kb(buttons)
            )
        except Exception:
            pass
        return

    # --- Button detail view ---
    if data in ("sb_btn1", "sb_btn2"):
        btn_key = data.split("_")[1]
        cfg     = load_config()
        btn     = cfg.get("buttons", {}).get(btn_key, {})
        try:
            await query.edit_message_text(
                _btn_detail_text(btn_key, btn),
                parse_mode="HTML",
                reply_markup=_setbutton_detail_kb(btn_key, btn)
            )
        except Exception:
            pass
        return

    # --- Button toggle ---
    if data in ("sb_btn1_toggle", "sb_btn2_toggle"):
        btn_key = data.split("_")[1]
        cfg     = load_config()
        buttons = cfg.setdefault("buttons", {})
        btn     = buttons.setdefault(btn_key, {})
        btn["enabled"]   = not btn.get("enabled", False)
        buttons[btn_key] = btn
        cfg["buttons"]   = buttons
        save_config(cfg)
        try:
            await query.edit_message_text(
                _btn_detail_text(btn_key, btn),
                parse_mode="HTML",
                reply_markup=_setbutton_detail_kb(btn_key, btn)
            )
        except Exception:
            pass
        return

    # --- Button rename ---
    if data in ("sb_btn1_rename", "sb_btn2_rename"):
        btn_key = data.split("_")[1]
        context.user_data["action"]     = f"sb_{btn_key}_wait_label"
        context.user_data["sb_btn_key"] = btn_key
        try:
            await query.edit_message_text(
                f"📝 <b>Button {btn_key[-1]}</b> ka naya naam type karo:\n(max 20 characters)",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    # --- Button link ---
    if data in ("sb_btn1_link", "sb_btn2_link"):
        btn_key = data.split("_")[1]
        context.user_data["action"]     = f"sb_{btn_key}_wait_link"
        context.user_data["sb_btn_key"] = btn_key
        try:
            await query.edit_message_text(
                f"🔗 <b>Button {btn_key[-1]}</b> ka link type karo:\n(https:// ya t.me/ se shuru hona chahiye)",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return


# =============================================================================
# MAIN
# =============================================================================
def main():
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable set nahi hai!")
    if ADMIN_ID == 0:
        raise ValueError("ADMIN_ID environment variable set nahi hai ya invalid hai!")

    try:
        init_db()
    except Exception as e:
        logger.error(f"DB init failed: {e}")
        raise

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",         cmd_start))
    app.add_handler(CommandHandler("help",          cmd_help))
    app.add_handler(CommandHandler("status",        cmd_status))
    app.add_handler(CommandHandler("manage",        cmd_manage))
    app.add_handler(CommandHandler("editgroup",     cmd_editgroup))
    app.add_handler(CommandHandler("rename",        cmd_rename))
    app.add_handler(CommandHandler("setbutton",     cmd_setbutton))
    app.add_handler(CommandHandler("setwatermark",  cmd_setwatermark))
    app.add_handler(CommandHandler("addgroup",      cmd_addgroup))
    app.add_handler(CommandHandler("addchannel",    cmd_addchannel))
    app.add_handler(CommandHandler("deletegroup",   cmd_deletegroup))
    app.add_handler(CommandHandler("deletechannel", cmd_deletechannel))
    app.add_handler(CommandHandler("testamz",       cmd_testamz))
    app.add_handler(CommandHandler("exportconfig",  cmd_exportconfig))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(
        filters.ALL & ~filters.COMMAND,
        handle_deal
    ))

    logger.info("DealsKoti Bot start ho raha hai...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()


"""
storage.py — PostgreSQL-backed persistent config storage.
"""
import os
import json
import copy
import logging
from contextlib import contextmanager

import psycopg2

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")

# Amazon post ke saare fields — har ek ka apna on/off
DEFAULT_AMZ_FIELDS = {
    "title":    True,
    "image":    True,
    "mrp":      True,
    "price":    True,
    "savings":  True,
    "discount": True,
    "rating":   True,
    "reviews":  True,
    "link":     True,
    "deal":     False,   # Lightning deal / deal badge + kab tak
    "stock":    False,   # Kitna stock bacha
    "seller":   False,   # Kaun bech raha hai
    "brand":    False,   # Brand ka naam
    "rank":     False,   # Best seller rank
    "features": False,   # 2 key features
}

# Caption mein kis order se fields bharenge (budget khatam hone tak)
AMZ_FIELD_ORDER = [
    "title", "deal", "mrp", "price", "savings", "discount",
    "rating", "reviews", "stock", "brand", "seller", "rank", "features",
]

DEFAULT_CONFIG = {
    "channel":        "",
    "source_channel": "",
    "silent":         True,
    "park_post":      False,
    "amz_detailed":   True,
    "amz_fields":     DEFAULT_AMZ_FIELDS,
    "header":         {"enabled": True,  "text": "🙏Jai Shree Ram Dosto🙏"},
    "footer":         {"enabled": False, "text": ""},
    "watermark":      {"enabled": True,  "text": "@DealKoti"},
    "buttons": {
        "btn1": {"label": "Join Channel", "url": "", "enabled": False},
        "btn2": {"label": "More Deals",   "url": "", "enabled": False},
        "buy":  {"label": "⚡ Buy Now",     "enabled": False},
        "cart": {"label": "🛒 Add to Cart", "enabled": False},
    },
}


def _get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable set nahi hai!")
    return psycopg2.connect(DATABASE_URL)


@contextmanager
def get_db():
    """Proper connection context manager — commits and always closes."""
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create tables on first run."""
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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS post_queue (
                    id         SERIAL PRIMARY KEY,
                    kind       TEXT      NOT NULL,
                    asin       TEXT,
                    payload    TEXT,
                    arrived_at TIMESTAMP NOT NULL,
                    tries      INTEGER   NOT NULL DEFAULT 0
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS post_queue_arrived_idx
                ON post_queue (arrived_at)
            """)
    logger.info("Database tables ready.")


def _fill_defaults(cfg: dict) -> dict:
    cfg.setdefault("channel", "")
    cfg.setdefault("source_channel", "")
    cfg.setdefault("silent", True)
    cfg.setdefault("park_post", False)
    cfg.setdefault("amz_detailed", True)

    fields = cfg.setdefault("amz_fields", {})
    for k, v in DEFAULT_AMZ_FIELDS.items():
        fields.setdefault(k, v)

    hdr = cfg.setdefault("header", {})
    hdr.setdefault("enabled", True)
    hdr.setdefault("text", "🙏Jai Shree Ram Dosto🙏")

    ftr = cfg.setdefault("footer", {})
    ftr.setdefault("enabled", False)
    ftr.setdefault("text", "")

    wm = cfg.setdefault("watermark", {})
    wm.setdefault("enabled", True)
    wm.setdefault("text", "@DealKoti")

    btns = cfg.setdefault("buttons", {})
    b1 = btns.setdefault("btn1", {})
    b1.setdefault("label", "Join Channel")
    b1.setdefault("url", "")
    b1.setdefault("enabled", False)
    b2 = btns.setdefault("btn2", {})
    b2.setdefault("label", "More Deals")
    b2.setdefault("url", "")
    b2.setdefault("enabled", False)
    buy = btns.setdefault("buy", {})
    buy.setdefault("label", "⚡ Buy Now")
    buy.setdefault("enabled", False)
    cart = btns.setdefault("cart", {})
    cart.setdefault("label", "🛒 Add to Cart")
    cart.setdefault("enabled", False)

    return cfg


def load_config() -> dict:
    """Load bot config from PostgreSQL. Missing keys default ho jaate hain."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM bot_config WHERE key = 'config'")
                row = cur.fetchone()
        if row:
            return _fill_defaults(json.loads(row[0]))
    except Exception as e:
        logger.error(f"Config load error: {e}")
    return copy.deepcopy(DEFAULT_CONFIG)


def save_config(config: dict) -> bool:
    """Persist bot config to PostgreSQL. True agar save ho gaya."""
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
        return True
    except Exception as e:
        logger.error(f"Config save error: {e}")
        return False

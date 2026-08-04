"""
storage.py — PostgreSQL-backed persistent config storage.
"""
import os
import json
import logging
from contextlib import contextmanager

import psycopg2

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")

DEFAULT_CONFIG = {
    "groups": [],
    "buttons": {
        "btn1": {"label": "Join Channel", "url": "", "enabled": False},
        "btn2": {"label": "More Deals",   "url": "", "enabled": False},
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


def _migrate_button_keys(config: dict) -> tuple:
    """Migrate old 'b1'/'b2' button keys to 'btn1'/'btn2'."""
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
    """Create tables on first run and migrate old button keys."""
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

    # Migrate old b1/b2 button keys if needed
    try:
        cfg = load_config()
        cfg, changed = _migrate_button_keys(cfg)
        if changed:
            save_config(cfg)
            logger.info("Button keys migrated b1/b2 → btn1/btn2.")
    except Exception as e:
        logger.error(f"Migration error: {e}")


def load_config() -> dict:
    """Load bot config from PostgreSQL. Returns default if not found."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM bot_config WHERE key = 'config'")
                row = cur.fetchone()
        if row:
            cfg = json.loads(row[0])
            cfg.setdefault("groups", [])
            cfg.setdefault("buttons", DEFAULT_CONFIG["buttons"].copy())
            return cfg
    except Exception as e:
        logger.error(f"Config load error: {e}")
    return {
        "groups": [],
        "buttons": {
            "btn1": {"label": "Join Channel", "url": "", "enabled": False},
            "btn2": {"label": "More Deals",   "url": "", "enabled": False},
        },
    }


def save_config(config: dict):
    """Persist bot config to PostgreSQL."""
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

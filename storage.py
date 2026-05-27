"""
storage.py — PostgreSQL-backed persistent config storage.
Replaces config.json so data survives Railway redeploys.
"""
import os
import json
import logging

import psycopg2
import psycopg2.extras

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
        raise RuntimeError(
            "DATABASE_URL environment variable set nahi hai! "
            "Railway pe Postgres service link karo."
        )
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    """Create tables on first run. Call once at startup."""
    with _get_conn() as conn:
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
        conn.commit()
    logger.info("Database tables ready.")


def load_config() -> dict:
    """Load bot config from PostgreSQL. Returns default if not found."""
    try:
        with _get_conn() as conn:
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
        "buttons": DEFAULT_CONFIG["buttons"].copy(),
    }


def save_config(config: dict):
    """Persist bot config to PostgreSQL."""
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO bot_config (key, value)
                    VALUES ('config', %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                    """,
                    (json.dumps(config, ensure_ascii=False),),
                )
            conn.commit()
    except Exception as e:
        logger.error(f"Config save error: {e}")

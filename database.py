"""
database.py — PostgreSQL-backed duplicate detection (title + caption based).
"""
import logging
import re
from datetime import datetime, timedelta

from storage import get_db

logger = logging.getLogger(__name__)

DUPLICATE_HOURS = 24

NOISE_WORDS = [
    'buy', 'shop', 'best price', 'order online', 'online', 'india',
    'get', 'deal', 'offer', 'discount', 'sale', 'free shipping',
    'lowest price', 'check price', 'view details', 'amazon', 'flipkart',
    'myntra', 'meesho', 'ajio', 'nykaa',
]


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
    """Remove seen_titles older than DUPLICATE_HOURS."""
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
    """
    Returns (True, "X ghante Y min pehle") if posted recently, else (False, None).
    """
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
    """Record a title/caption as posted (upsert)."""
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

"""
database.py — duplicate detection + parked post queue.
"""
import re
import json
import logging
from datetime import datetime, timedelta

from storage import get_db

logger = logging.getLogger(__name__)

DUPLICATE_WINDOW_HOURS = 24
CLEANUP_AFTER_HOURS    = 72

_PUNCT_RE = re.compile(r"[^a-z0-9\s]")
_WS_RE    = re.compile(r"\s+")


# =============================================================================
# DUPLICATE DETECTION
# =============================================================================
def _normalise(text: str) -> str:
    """Title/text ko ek saaf key mein badlo taaki chhote farak se dhoka na ho."""
    if not text:
        return ""
    low = text.lower().strip()
    low = _PUNCT_RE.sub(" ", low)
    low = _WS_RE.sub(" ", low).strip()
    return low[:300]


def _human_gap(then: datetime) -> str:
    delta = datetime.now() - then
    mins  = int(delta.total_seconds() // 60)
    if mins < 1:
        return "abhi abhi"
    if mins < 60:
        return f"{mins} minute pehle"
    hours = mins // 60
    if hours < 24:
        return f"{hours} ghante pehle"
    return f"{hours // 24} din pehle"


def is_duplicate(text: str):
    """(True, "2 ghante pehle") agar ye pehle post ho chuka hai."""
    key = _normalise(text)
    if not key:
        return False, None
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT posted_at FROM seen_titles WHERE title_key = %s",
                    (key,),
                )
                row = cur.fetchone()
        if row:
            posted_at = row[0]
            if datetime.now() - posted_at < timedelta(hours=DUPLICATE_WINDOW_HOURS):
                return True, _human_gap(posted_at)
    except Exception as e:
        logger.error(f"Duplicate check error: {e}")
    return False, None


def mark_posted(text: str):
    key = _normalise(text)
    if not key:
        return
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO seen_titles (title_key, posted_at)
                    VALUES (%s, %s)
                    ON CONFLICT (title_key) DO UPDATE SET posted_at = EXCLUDED.posted_at
                    """,
                    (key, datetime.now()),
                )
    except Exception as e:
        logger.error(f"Mark posted error: {e}")


def cleanup_old_entries():
    try:
        cutoff = datetime.now() - timedelta(hours=CLEANUP_AFTER_HOURS)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM seen_titles WHERE posted_at < %s", (cutoff,))
    except Exception as e:
        logger.error(f"Cleanup error: {e}")


# =============================================================================
# POST QUEUE (park mode)
# =============================================================================
def queue_add_amazon(asin: str) -> bool:
    """ASIN ko queue mein daalo. False agar pehle se pada hai."""
    if not asin:
        return False
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM post_queue WHERE kind = 'amazon' AND asin = %s",
                    (asin,),
                )
                if cur.fetchone():
                    return False
                cur.execute(
                    """
                    INSERT INTO post_queue (kind, asin, payload, arrived_at)
                    VALUES ('amazon', %s, NULL, %s)
                    """,
                    (asin, datetime.now()),
                )
        return True
    except Exception as e:
        logger.error(f"Queue add (amazon) error: {e}")
        return False


def queue_add_other(payload: dict) -> bool:
    """Non-Amazon post ko queue mein daalo (text + entities + file_id)."""
    payload = payload or {}
    if not (payload.get("text") or "").strip() \
            and not payload.get("photo_file_id") \
            and not payload.get("media_file_id"):
        logger.warning("Queue add (other) skip — khali payload")
        return False
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO post_queue (kind, asin, payload, arrived_at)
                    VALUES ('other', NULL, %s, %s)
                    """,
                    (json.dumps(payload, ensure_ascii=False), datetime.now()),
                )
        return True
    except Exception as e:
        logger.error(f"Queue add (other) error: {e}")
        return False


def queue_fetch_all() -> list:
    """Saare parked items, arrival order mein."""
    rows_out = []
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, kind, asin, payload, arrived_at, tries
                    FROM post_queue ORDER BY arrived_at ASC, id ASC
                    """
                )
                rows = cur.fetchall()
        for r in rows:
            payload = {}
            if r[3]:
                try:
                    payload = json.loads(r[3])
                except Exception:
                    payload = {}
            rows_out.append({
                "id":         r[0],
                "kind":       r[1],
                "asin":       r[2],
                "payload":    payload,
                "arrived_at": r[4],
                "tries":      r[5],
            })
    except Exception as e:
        logger.error(f"Queue fetch error: {e}")
    return rows_out


def queue_delete(item_id: int):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM post_queue WHERE id = %s", (item_id,))
    except Exception as e:
        logger.error(f"Queue delete error: {e}")


def queue_bump_tries(item_id: int) -> int:
    """tries +1 karo, nayi value do."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE post_queue SET tries = tries + 1 WHERE id = %s RETURNING tries",
                    (item_id,),
                )
                row = cur.fetchone()
        return row[0] if row else 0
    except Exception as e:
        logger.error(f"Queue bump error: {e}")
        return 0


def queue_purge_old(max_age_hours: int = 4) -> int:
    """Purani deals hata do — mar chuki hongi. Kitni hatai wo batao."""
    try:
        cutoff = datetime.now() - timedelta(hours=max_age_hours)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM post_queue WHERE arrived_at < %s RETURNING id",
                    (cutoff,),
                )
                return len(cur.fetchall())
    except Exception as e:
        logger.error(f"Queue purge error: {e}")
        return 0


def queue_clear() -> int:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM post_queue RETURNING id")
                return len(cur.fetchall())
    except Exception as e:
        logger.error(f"Queue clear error: {e}")
        return 0


def queue_counts() -> tuple:
    """(amazon_count, other_count)"""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT kind, COUNT(*) FROM post_queue GROUP BY kind"
                )
                rows = dict(cur.fetchall())
        return rows.get("amazon", 0), rows.get("other", 0)
    except Exception as e:
        logger.error(f"Queue count error: {e}")
        return 0, 0

import json
import os
import re
from datetime import datetime, timedelta

DB_FILE = "database.json"
DUPLICATE_HOURS = 24

NOISE_WORDS = [
    'buy', 'shop', 'best price', 'order online', 'online', 'india',
    'get', 'deal', 'offer', 'discount', 'sale', 'free shipping',
    'lowest price', 'check price', 'view details', 'amazon', 'flipkart',
    'myntra', 'meesho', 'ajio', 'nykaa', 'at', 'in', 'on', 'the', 'a',
]


def load_db():
    if not os.path.exists(DB_FILE):
        return {}
    try:
        with open(DB_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_db(db):
    try:
        with open(DB_FILE, 'w', encoding='utf-8') as f:
            json.dump(db, f, indent=2, default=str, ensure_ascii=False)
    except Exception as e:
        print(f"DB save error: {e}")


def clean_title(title):
    if not title:
        return ""
    title = title.lower().strip()
    for word in NOISE_WORDS:
        title = title.replace(word, ' ')
    title = re.sub(r'[^\w\s]', ' ', title)
    title = re.sub(r'\s+', ' ', title).strip()
    return title


def cleanup_old_entries():
    db = load_db()
    cutoff = datetime.now() - timedelta(hours=DUPLICATE_HOURS)
    cleaned = {
        k: v for k, v in db.items()
        if datetime.fromisoformat(v) > cutoff
    }
    save_db(cleaned)


def is_duplicate(title):
    if not title:
        return False, None
    cleaned = clean_title(title)
    if not cleaned:
        return False, None
    db = load_db()
    if cleaned in db:
        try:
            posted_at = datetime.fromisoformat(db[cleaned])
            diff = datetime.now() - posted_at
            if diff < timedelta(hours=DUPLICATE_HOURS):
                hours_ago = int(diff.total_seconds() / 3600)
                mins_ago = int((diff.total_seconds() % 3600) / 60)
                if hours_ago == 0:
                    time_str = f"{mins_ago} minutes pehle"
                else:
                    time_str = f"{hours_ago} ghante {mins_ago} min pehle"
                return True, time_str
        except Exception:
            pass
    return False, None


def mark_posted(title):
    if not title:
        return
    cleaned = clean_title(title)
    if not cleaned:
        return
    db = load_db()
    db[cleaned] = datetime.now().isoformat()
    save_db(db)

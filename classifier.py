import logging
from keywords import keyword_category

logger = logging.getLogger(__name__)


async def detect_category(text: str):
    """
    Keyword-based category detection (no AI).
    Returns: (category, method, ai_error, matched_keywords)
    """
    if not text or not text.strip():
        return None, "None", None, []

    kw_cat, matched_kws = keyword_category(text)
    if kw_cat:
        logger.info(f"Keyword category: {kw_cat} | matched: {matched_kws}")
        return kw_cat, "Keyword", None, matched_kws

    logger.warning("Koi category detect nahi hui")
    return None, "None", None, []

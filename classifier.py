import os
import json
import logging
import aiohttp
from keywords import keyword_category, CATEGORIES

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a product category classifier for Indian e-commerce deals (Amazon, Flipkart, Meesho, etc.).

Your job: Read the deal message and pick EXACTLY ONE category from this list:

- fitness     → gym equipment, protein/whey/creatine, yoga mat, dumbbell, treadmill, sports nutrition, supplement, health drink
- fashion     → clothing, shirt, jeans, kurti, saree, shoes, sneakers, boots, sandals, bag, wallet, handbag, watch, sunglasses, cap, belt, ethnic wear
- electronics → phone, laptop, TV, headphones, earphones, speaker, charger, smartwatch, camera, powerbank, router, trimmer, mixer, AC, fridge, washing machine
- home        → kitchen items, cookware, key holder, organizer, wall decor, shelf, furniture, bedsheet, pillow, curtain, cleaning items, grocery, food, baby items, stationery, toys, home decor, wooden items for home
- skincare    → face wash, face scrub, moisturizer, serum, sunscreen, shampoo, hair oil, lipstick, foundation, perfume, deodorant, beard oil, makeup, beauty products, acne, pimple

Rules:
- Reply with ONLY the category word in lowercase. No explanation, no punctuation, nothing else.

Examples:
"wooden key holder wall mount" → home
"Nike running shoes" → fashion
"boAt earphones 20% off" → electronics
"whey protein 2kg" → fitness
"mamaearth face scrub" → skincare
"bedsheet combo set" → home"""


async def detect_category(text: str):
    """
    AI se category detect karo. Agar AI fail ho to keywords fallback.
    Returns: (category, method, ai_error, matched_keywords)
    """
    if not text or not text.strip():
        return None, "None", None, []

    ai_error = None
    api_key = os.getenv("OPENAI_API_KEY", "").strip()

    # ── AI try karo (direct HTTP — SDK bypass) ──
    if api_key:
        try:
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": text[:1000]}
                ],
                "max_tokens": 10,
                "temperature": 0
            }
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers=headers,
                    json=payload
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        ai_result = data["choices"][0]["message"]["content"].strip().lower().strip(".")
                        if ai_result in CATEGORIES:
                            logger.info(f"AI category: {ai_result}")
                            return ai_result, "AI", None, []
                        else:
                            ai_error = f"AI ne galat response diya: '{ai_result}'"
                            logger.warning(ai_error)
                    else:
                        body = await resp.text()
                        ai_error = f"HTTP {resp.status}: {body[:200]}"
                        logger.error(f"OpenAI error: {ai_error}")
        except Exception as e:
            ai_error = str(e)
            logger.error(f"AI call fail: {e}")
    else:
        ai_error = "OPENAI_API_KEY set nahi hai"

    # ── Keyword fallback ──
    kw_cat, matched_kws = keyword_category(text)
    if kw_cat:
        logger.info(f"Keyword category: {kw_cat} | matched: {matched_kws}")
        return kw_cat, "Keyword", ai_error, matched_kws

    logger.warning("Koi category detect nahi hui")
    return None, "None", ai_error, []

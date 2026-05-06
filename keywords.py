FASHION_KEYWORDS = [
    "sneaker", "sneakers", "shoe", "shoes", "boot", "boots", "sandal",
    "sandals", "slipper", "slippers", "footwear", "heel", "heels",
    "loafer", "loafers", "red tape", "bata", "nike", "adidas", "puma",
    "reebok", "skechers", "woodland", "liberty", "crocs", "shirt",
    "t-shirt", "tshirt", "jeans", "trouser", "trousers", "dress", "kurti",
    "saree", "lehenga", "jacket", "coat", "sweater", "hoodie",
    "blouse", "skirt", "shorts", "track pant", "trackpant", "legging",
    "leggings", "handbag", "purse", "wallet", "wristwatch", "sunglasses",
    "belt", "baseball cap", "sports cap", "men cap", "women cap",
    "scarf", "clothing", "apparel", "fashion",
    "ethnic wear", "kurta", "sherwani", "salwar", "dupatta",
    "levi", "zara", "h&m", "myntra", "westside", "pantaloons"
]

ELECTRONICS_KEYWORDS = [
    "speaker", "bluetooth speaker", "earphone", "earphones", "headphone",
    "headphones", "earbud", "earbuds", "airpods", "tws", "neckband",
    "tv", "television", "smart tv", "led tv", "laptop", "mobile", "phone",
    "smartphone", "camera", "charger", "powerbank", "power bank",
    "smartwatch", "tablet", "monitor", "keyboard", "mouse", "router",
    "led bulb", "projector", "pendrive", "pen drive", "hard disk", "ssd",
    "trimmer", "shaver", "electric shaver", "iron", "steam iron", "mixer",
    "juicer", "microwave", "oven", "induction", "air cooler", "ac",
    "refrigerator", "fridge", "washing machine", "vacuum cleaner",
    "air purifier", "water purifier", "ro", "fire stick", "echo", "alexa",
    "boat", "jbl", "sony", "samsung electronics", "mi", "realme", "oneplus",
    "apple watch", "dell", "hp", "lenovo", "asus", "graphics card",
    "processor", "ram", "gaming", "gamepad", "controller", "webcam",
    "printer", "scanner", "ups", "inverter", "stabilizer", "cable",
    "adapter", "hub", "dongle"
]

FITNESS_KEYWORDS = [
    "protein", "whey", "creatine", "supplement", "bcaa", "pre workout",
    "preworkout", "gym", "dumbbell", "barbell", "resistance band",
    "yoga mat", "treadmill", "cycle", "fitness band", "mass gainer",
    "weight gainer", "health drink", "multivitamin", "omega", "fish oil",
    "weight loss", "fat burner", "amino acid", "sports nutrition",
    "muscle", "workout", "exercise", "fitness equipment"
]

SKINCARE_KEYWORDS = [
    "face wash", "face scrub", "face pack", "face mask", "face cream",
    "face serum", "face gel", "face toner", "face mist", "face oil",
    "moisturizer", "serum", "sunscreen", "spf", "toner",
    "cleanser", "body lotion", "body scrub", "body butter", "body oil",
    "shampoo", "conditioner", "hair oil", "hair mask", "hair serum",
    "hair gel", "hair wax", "hair color", "hair dye",
    "lip balm", "lip gloss", "lip liner", "lip care",
    "foundation", "concealer", "mascara", "eyeliner", "lipstick",
    "kajal", "blush", "highlighter", "primer", "compact", "bb cream",
    "mamaearth", "wow skincare", "minimalist", "dot & key", "plum",
    "lakme", "loreal", "garnier", "himalaya", "biotique", "cetaphil",
    "neutrogena", "the ordinary", "forest essentials", "kama ayurveda",
    "nivea", "vaseline", "ponds", "olay", "fair & lovely", "glow & lovely",
    "skincare", "skin care", "anti aging", "anti-aging",
    "body wash", "shower gel", "perfume", "deodorant", "deo",
    "makeup", "cosmetics", "beauty", "hair care", "hair growth",
    "dandruff", "beard oil", "beard care", "beard balm",
    "eye cream", "under eye", "exfoliator", "exfoliate", "scrub",
    "peel off", "peel mask", "clay mask", "sheet mask", "night cream",
    "day cream", "spf cream", "acne", "pimple", "dark spot",
    "pigmentation", "brightening", "whitening", "tan removal",
    "vitamin c", "niacinamide", "hyaluronic", "retinol", "salicylic"
]

HOME_KEYWORDS = [
    "kitchen", "cookware", "pressure cooker", "kadai", "tawa", "pan",
    "pot", "casserole", "container", "storage box", "bottle", "flask",
    "thermos", "bedsheet", "pillow", "blanket", "curtain", "mat",
    "doormat", "sofa", "chair", "table", "shelf", "rack", "organizer",
    "cleaning", "mop", "broom", "dustbin", "laundry", "detergent",
    "grocery", "food", "snack", "biscuit", "namkeen", "ghee",
    "dal", "rice", "atta", "masala", "spice", "tea", "coffee",
    "pickle", "achar", "achaar", "chutney", "ketchup", "vinegar",
    "cooking oil", "mustard oil", "coconut oil", "olive oil", "sunflower oil",
    "health food", "dry fruit", "nuts", "honey", "jam", "sauce", "noodles",
    "pasta", "soup", "instant food", "ready to eat", "chips", "popcorn",
    "decoration", "decor", "lamp", "light fitting", "garden", "plant",
    "tool", "hardware", "paint", "lock", "door", "window", "stationery",
    "notebook", "pen", "pencil", "toy", "game board", "puzzle",
    "baby", "diaper", "baby food", "pram", "stroller",
    "key holder", "keyholder", "key organizer", "key rack",
    "wooden", "wall mount", "wall art", "wall decor", "wall hook",
    "home decor", "home decoration", "room decor", "hanging",
    "photo frame", "picture frame", "clock", "wall clock",
    "candle", "vase", "flower pot", "showpiece", "figurine",
    "bed cover", "quilt", "mattress", "towel", "napkin",
    "water bottle", "lunch box", "tiffin", "dinner set", "glass set",
    "chopping board", "spatula", "ladle", "rolling pin", "grater",
    "trash can", "shoe rack", "cloth hanger", "iron board",
    "led strip", "night lamp", "table lamp", "bulb holder"
]

CATEGORIES = ["fitness", "fashion", "electronics", "home", "skincare"]


def keyword_category(text: str):
    """
    Returns (category, matched_keywords_list)
    """
    t = text.lower()
    all_kws = {
        "fashion":     FASHION_KEYWORDS,
        "electronics": ELECTRONICS_KEYWORDS,
        "fitness":     FITNESS_KEYWORDS,
        "skincare":    SKINCARE_KEYWORDS,
        "home":        HOME_KEYWORDS,
    }
    scores = {}
    matched = {}
    for cat, kws in all_kws.items():
        found = [kw for kw in kws if kw in t]
        scores[cat] = len(found)
        matched[cat] = found

    best = max(scores, key=scores.get)
    if scores[best] > 0:
        return best, matched[best]
    return None, []

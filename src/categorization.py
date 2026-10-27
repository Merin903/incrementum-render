"""Rule-based product -> department classification, with manual correction.

Products that match no keyword, or keywords from TWO departments, are NOT forced into a
department: they become 'Other / Uncertain' with needs_review=1 so a human can fix them.
"""
import re

from . import config

KEYWORDS = {
    "Non-Food & Household": ["DETTOL", "HARPIC", "SURF", "DETERGENT", "CLEANER", "SOAP", "BLEACH",
                             "DISINFECTANT", "ANTISEPTIC", "TISSUE", "SHAMPOO", "TOOTHPASTE", "LIZOL"],
    "Frozen & Snacks": ["COCA-COLA", "COLA", "PEPSI", "CHIPS", "BISCUIT", "COOKIES", "CHOCOLATE", "NOODLES",
                        "ICE CREAM", "FROZEN", "SNACK", "SODA", "JUICE", "NAMKEEN"],
    "Pantry & Dry Goods": ["RICE", "OIL", "DAL", "SUGAR", "FLOUR", "ATTA", "SALT", "SPICE", "TEA", "COFFEE",
                           "PASTA", "LENTIL", "MASALA"],
    "Fresh & Chilled": ["MILK", "CURD", "YOGURT", "EGG", "EGGS", "BREAD", "BUTTER", "CHEESE", "PANEER",
                        "VEGETABLE", "FRUIT", "CHICKEN", "FISH", "FRESH"],
}


# Categories for the KAGGLE gift-ware retailer (the four supermarket departments do not describe it).
# These are DERIVED labels from product names - the dataset has no category column.
GIFTWARE_KEYWORDS = {
    "Seasonal & Celebrations": ["CHRISTMAS", "XMAS", "EASTER", "HALLOWEEN", "ADVENT", "SANTA", "SNOWMAN",
                                "REINDEER", "BUNNY", "WREATH", "BIRTHDAY", "VALENTINE"],
    "Lights & Candles": ["LIGHT", "T-LIGHT", "TEALIGHT", "CANDLE", "LANTERN", "OIL BURNER", "INCENSE", "LAMP"],
    "Kitchen & Dining": ["MUG", "CUP", "PLATE", "BOWL", "TEAPOT", "TEA TOWEL", "TEA COSY", "JUG", "SPOON", "APRON",
                         "BAKING", "COOKIE CUTTER", "NAPKIN", "COASTER", "BUTTER DISH", "SUGAR", "TEA STRAINER",
                         "BREAD BIN", "FRYING PAN", "LUNCH BOX", "PLACEMAT", "CUTLERY", "KITCHEN", "CAKE STAND"],
    "Bags & Storage": ["BAG", "BOX", "TIN", "JAR", "BASKET", "STORAGE", "CASE", "CONTAINER", "PURSE", "WALLET",
                       "TRUNK", "CRATE"],
    "Stationery & Party": ["CARD", "WRAP", "PAPER", "PENCIL", "NOTEBOOK", "PARTY", "BALLOON", "BUNTING", "RIBBON",
                           "STICKER", "GIFT TAG", "ENVELOPE", "CRAYON", "STAMP"],
    "Home Decor": ["FRAME", "CLOCK", "MIRROR", "CUSHION", "SIGN", "DECORATION", "HANGING", "DOORMAT", "WALL",
                   "HOOK", "VASE", "DOOR STOP", "THROW", "RUG", "CURTAIN", "MAGNET", "ORNAMENT", "FIGURINE",
                   "PHOTO", "SHELF"],
    "Toys & Games": ["TOY", "GAME", "DOLL", "PUZZLE", "PLAYING CARDS", "SKITTLES", "DOMINOES", "TEDDY"],
    "Garden & Outdoor": ["GARDEN", "PLANT", "BIRD", "SEED", "PARASOL", "WATERING", "WINDMILL"],
    "Bath & Personal": ["SOAP", "SPONGE", "BATH", "WASHBAG", "WASH BAG", "HOT WATER BOTTLE", "BATH TOWEL"],
}


def _hit_counts(text, table):
    hits = {}
    for dept, words in table.items():
        n = sum(1 for w in words if re.search(rf"(?<![A-Z]){re.escape(w)}S?(?![A-Z])", text))
        if n:
            hits[dept] = n
    return hits


def classify_giftware(description):
    """Gift-ware category. One clear winner -> assign; tie/no match -> 'Other / Uncertain' for review."""
    hits = _hit_counts((description or "").upper(), GIFTWARE_KEYWORDS)
    if not hits:
        return config.UNCERTAIN, 0.0, 1
    ranked = sorted(hits.items(), key=lambda kv: -kv[1])
    if len(ranked) == 1:
        cat, n = ranked[0]
        return cat, (0.95 if n >= 2 else 0.85), 0
    if ranked[0][1] > ranked[1][1]:          # clear majority among competing categories -> keep, but flag
        return ranked[0][0], 0.6, 1
    return config.UNCERTAIN, 0.0, 1


def classify(description):
    """SUPERMARKET departments (used for the synthetic demo products). Returns (department, confidence, needs_review)."""
    text = (description or "").upper()
    hits = {}
    for dept, words in KEYWORDS.items():
        n = sum(1 for w in words if re.search(rf"(?<![A-Z]){re.escape(w)}(?![A-Z])", text))
        if n:
            hits[dept] = n
    if len(hits) == 1:
        dept, n = next(iter(hits.items()))
        return dept, (0.95 if n >= 2 else 0.85), 0
    return config.UNCERTAIN, 0.0, 1          # no match, or conflicting departments


def apply_to_products(conn, synthetic_only=False) -> dict:
    """Classify products that have not been manually corrected (optionally only the sample catalogue products)."""
    rows = conn.execute("SELECT stock_code, description, is_synthetic FROM products WHERE category_source = 'rule'"
                        + (" AND is_synthetic = 1" if synthetic_only else "")).fetchall()
    for r in rows:   # demo products -> supermarket departments; Kaggle products -> gift-ware categories
        dept, conf, review = (classify if r["is_synthetic"] else classify_giftware)(r["description"])
        conn.execute("UPDATE products SET department=?, category_confidence=?, needs_review=? WHERE stock_code=?",
                     (dept, conf, review, r["stock_code"]))
    conn.commit()
    return dict(conn.execute("SELECT department, COUNT(*) FROM products GROUP BY department").fetchall())


def set_manual(conn, stock_code, department) -> None:
    """Operator correction. Manual assignments are never overwritten by apply_to_products."""
    if department not in config.DEPARTMENTS + list(GIFTWARE_KEYWORDS) + [config.UNCERTAIN]:
        raise ValueError(f"Unknown department: {department}")
    cur = conn.execute(
        "UPDATE products SET department=?, category_source='manual', category_confidence=1.0, needs_review=0 "
        "WHERE stock_code=?", (department, stock_code))
    if cur.rowcount == 0:
        raise ValueError(f"Unknown product: {stock_code}")
    conn.commit()


def review_list(conn) -> list:
    return conn.execute("SELECT stock_code, description, department, category_confidence FROM products "
                        "WHERE needs_review = 1 ORDER BY stock_code").fetchall()

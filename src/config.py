"""Central configuration. Every other module imports paths and settings from here."""
import os
import re
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")  # secrets (phone numbers, Twilio keys) live in .env, never in code/Git

RAW_FILE = ROOT / "data" / "raw" / "online_retail_II.csv"
CLEANED_DIR = ROOT / "data" / "cleaned"
POWERBI_DIR = ROOT / "data" / "powerbi_exports"
REPORTS_DIR = ROOT / "outputs" / "reports"
LOGS_DIR = ROOT / "outputs" / "logs"
SCHEMA_FILE = ROOT / "sql" / "schema.sql"
CONFIG_DIR = ROOT / "config"
ACTIVE_OFFERS_FILE = CONFIG_DIR / "active_offers.json"
RECIPIENTS_FILE = CONFIG_DIR / "demo_recipients.json"
OFFER_RULES_FILE = CONFIG_DIR / "offer_rules.json"

_db = os.getenv("DB_PATH", "data/incrementum.db")
DB_PATH = Path(_db) if Path(_db).is_absolute() else ROOT / _db

HDFS_URI = os.getenv("HDFS_URI", "hdfs://localhost:9000")   # must match fs.defaultFS in core-site.xml
HDFS_BASE = "/incrementum"
HDFS_RAW = f"{HDFS_BASE}/raw"
HDFS_CLEANED = f"{HDFS_BASE}/cleaned"
HDFS_PROCESSED = f"{HDFS_BASE}/processed"
PROCESSED_DIR = ROOT / "data" / "processed"

# ---- Departments ----------------------------------------------------------
DEPARTMENTS = [
    "Non-Food & Household",
    "Frozen & Snacks",
    "Pantry & Dry Goods",
    "Fresh & Chilled",
]
UNCERTAIN = "Other / Uncertain"
ALL_CATEGORIES = "All categories"      # Send Offers choice: check every department in one campaign

# ---- Cleaning rules (Kaggle Online Retail II) ----------------------------
# Stock codes that are fees/adjustments/postage, not real products.
NON_PRODUCT_CODES = {
    "POST", "D", "M", "DOT", "BANK CHARGES", "ADJUST", "ADJUST2", "CRUK", "C2",
    "S", "B", "AMAZONFEE", "SAMPLES", "PADS", "TEST001", "TEST002",
}
NON_PRODUCT_PATTERN = re.compile(r"^(GIFT_\d+.*|DCGS.*|TEST\d*)$")
VALID_INVOICE = re.compile(r"^\d{6}$")        # normal sales invoices are exactly 6 digits
VALID_CUSTOMER = re.compile(r"^\d{4,7}$")     # numeric customer ids
OUTLIER_QUANTILE = 0.999                      # values above this are FLAGGED, never removed

# ---- Preference analysis --------------------------------------------------
LOOKBACK_DAYS = 90        # only purchases in this window count
MIN_PURCHASES = 2         # distinct invoices needed to call a product "frequently purchased"
MAX_OFFERS_PER_MESSAGE = 3

# ---- Notifications --------------------------------------------------------
# Real sending is OFF unless WHATSAPP_ENABLED=true in .env AND the operator confirms at send time.
WHATSAPP_ENABLED = os.getenv("WHATSAPP_ENABLED", "false").lower() == "true"
SHOP_NAME = os.getenv("SHOP_NAME", "Incrementum Store")
MESSAGE_FOOTER = os.getenv("MESSAGE_FOOTER", "(Incrementum project test message)")   # honest sign-off on every message

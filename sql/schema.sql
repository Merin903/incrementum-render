-- Incrementum relational schema (SQLite; ports to PostgreSQL with minor type changes)
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS processing_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage TEXT NOT NULL,
    execution_mode TEXT NOT NULL CHECK (execution_mode IN ('A_hadoop_spark','B_local_python')),
    started_at TEXT NOT NULL, finished_at TEXT, duration_seconds REAL,
    rows_in INTEGER, rows_out INTEGER,
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','success','failed')),
    notes TEXT
);

CREATE TABLE IF NOT EXISTS data_cleaning_logs (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES processing_runs(run_id),
    step_order INTEGER NOT NULL, step_name TEXT NOT NULL, decision TEXT NOT NULL,
    rows_before INTEGER, rows_after INTEGER, rows_affected INTEGER, logged_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    customer_id  TEXT PRIMARY KEY,
    country      TEXT,
    display_name TEXT,
    is_synthetic INTEGER NOT NULL DEFAULT 0 CHECK (is_synthetic IN (0,1)),
    source       TEXT NOT NULL DEFAULT 'kaggle_online_retail_ii'
);

CREATE TABLE IF NOT EXISTS products (
    stock_code          TEXT PRIMARY KEY,
    description         TEXT,
    department          TEXT NOT NULL DEFAULT 'Other / Uncertain',
    category_confidence REAL,
    category_source     TEXT NOT NULL DEFAULT 'rule' CHECK (category_source IN ('rule','manual')),
    needs_review        INTEGER NOT NULL DEFAULT 0 CHECK (needs_review IN (0,1)),
    is_synthetic        INTEGER NOT NULL DEFAULT 0 CHECK (is_synthetic IN (0,1))
);

CREATE TABLE IF NOT EXISTS transactions (
    invoice      TEXT PRIMARY KEY,
    customer_id  TEXT NOT NULL REFERENCES customers(customer_id),
    invoice_date TEXT NOT NULL,
    country      TEXT,
    is_synthetic INTEGER NOT NULL DEFAULT 0 CHECK (is_synthetic IN (0,1))
);

CREATE TABLE IF NOT EXISTS transaction_items (
    item_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice    TEXT NOT NULL REFERENCES transactions(invoice),
    stock_code TEXT NOT NULL REFERENCES products(stock_code),
    quantity   INTEGER NOT NULL CHECK (quantity > 0),
    price      REAL NOT NULL CHECK (price > 0),
    line_total REAL NOT NULL,
    is_synthetic INTEGER NOT NULL DEFAULT 0 CHECK (is_synthetic IN (0,1))
);
CREATE INDEX IF NOT EXISTS idx_items_invoice ON transaction_items(invoice);
CREATE INDEX IF NOT EXISTS idx_items_stock ON transaction_items(stock_code);
CREATE INDEX IF NOT EXISTS idx_tx_customer ON transactions(customer_id);

CREATE TABLE IF NOT EXISTS customer_features (
    customer_id TEXT PRIMARY KEY REFERENCES customers(customer_id),
    recency_days INTEGER, frequency INTEGER, monetary REAL, avg_basket_value REAL,
    avg_quantity REAL, last_purchase_date TEXT, top_category TEXT, top_product TEXT,
    avg_reorder_interval_days REAL, computed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS product_preference_segments (
    segment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_name TEXT NOT NULL UNIQUE, segment_type TEXT NOT NULL,
    product_or_category TEXT, rule_description TEXT NOT NULL, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customer_segment_memberships (
    membership_id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    segment_id INTEGER NOT NULL REFERENCES product_preference_segments(segment_id),
    strength REAL, reason TEXT NOT NULL,
    UNIQUE (customer_id, segment_id)
);

-- Demo recipients: a consenting person mapped to a synthetic demo customer.
-- The phone number itself is NEVER stored; only the name of the .env variable.
CREATE TABLE IF NOT EXISTS demo_recipients (
    recipient_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    customer_id TEXT NOT NULL UNIQUE REFERENCES customers(customer_id),
    phone_env_key TEXT NOT NULL,
    consent_status TEXT NOT NULL DEFAULT 'unknown' CHECK (consent_status IN ('unknown','opted_in','opted_out')),
    consent_recorded_at TEXT
);

CREATE TABLE IF NOT EXISTS offers (
    offer_id INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_code TEXT NOT NULL UNIQUE,
    stock_code TEXT NOT NULL REFERENCES products(stock_code),
    department TEXT NOT NULL,
    discount_percent REAL CHECK (discount_percent IS NULL OR (discount_percent >= 0 AND discount_percent <= 100)),
    promo_text TEXT,
    valid_from TEXT, valid_to TEXT,
    origin TEXT NOT NULL CHECK (origin IN ('configured','generated')),
    status TEXT NOT NULL CHECK (status IN ('active','inactive','proposed','rejected')),
    created_at TEXT NOT NULL
);

-- Part 2: generated offers are proposals until a human approves them.
CREATE TABLE IF NOT EXISTS offer_proposals (
    proposal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id INTEGER NOT NULL UNIQUE REFERENCES offers(offer_id),
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    stock_code TEXT NOT NULL REFERENCES products(stock_code),
    department TEXT NOT NULL,
    purchase_count INTEGER NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','approved','rejected')),
    created_at TEXT NOT NULL, decided_at TEXT, notified_at TEXT
);

CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_key TEXT NOT NULL UNIQUE,           -- idempotency key: same key = same campaign
    name TEXT NOT NULL,
    strategy TEXT NOT NULL,
    department TEXT,
    source TEXT NOT NULL CHECK (source IN ('configured_active','approved_generated')),
    delivery_mode TEXT NOT NULL CHECK (delivery_mode IN ('simulated','whatsapp')),
    rehearsal INTEGER NOT NULL DEFAULT 0 CHECK (rehearsal IN (0,1)),
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','completed','failed')),
    created_at TEXT NOT NULL, finished_at TEXT
);

CREATE TABLE IF NOT EXISTS campaign_recipients (
    recipient_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(campaign_id),
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    status TEXT NOT NULL, reason TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE (campaign_id, customer_id)            -- one notification per customer per campaign
);

CREATE TABLE IF NOT EXISTS notification_logs (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(campaign_id),
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    recipient_name TEXT,
    strategy TEXT, department TEXT,
    matched_offers TEXT,                          -- JSON list of offer codes
    product_or_category TEXT,
    selection_reason TEXT,
    skip_or_suppression_reason TEXT,
    message_text TEXT,
    delivery_mode TEXT NOT NULL CHECK (delivery_mode IN ('simulated','whatsapp')),
    attempted_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('simulated','queued','sent_to_provider','delivered','suppressed','skipped','failed')),
    provider_message_id TEXT, provider_status TEXT, failure_reason TEXT
);

CREATE TABLE IF NOT EXISTS campaign_experiments (
    experiment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER REFERENCES campaigns(campaign_id),
    name TEXT NOT NULL, hypothesis TEXT, eligibility_rules TEXT, assignment_method TEXT,
    observation_window_days INTEGER NOT NULL DEFAULT 14, outcome_definition TEXT,
    is_simulated INTEGER NOT NULL DEFAULT 1 CHECK (is_simulated IN (0,1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS experiment_assignments (
    assignment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES campaign_experiments(experiment_id),
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    group_name TEXT NOT NULL CHECK (group_name IN ('treatment','control')),
    assigned_at TEXT NOT NULL, UNIQUE (experiment_id, customer_id)
);
CREATE TABLE IF NOT EXISTS experiment_outcomes (
    outcome_id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES campaign_experiments(experiment_id),
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    purchased INTEGER NOT NULL CHECK (purchased IN (0,1)),
    purchase_date TEXT, revenue REAL,
    is_simulated INTEGER NOT NULL DEFAULT 1 CHECK (is_simulated IN (0,1)),
    UNIQUE (experiment_id, customer_id)
);
CREATE TABLE IF NOT EXISTS cause_based_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    experiment_id INTEGER REFERENCES campaign_experiments(experiment_id),
    segment_label TEXT NOT NULL CHECK (segment_label IN ('Persuadable','Sure Thing','Lost Cause','Do-not-disturb')),
    evidence_level TEXT NOT NULL CHECK (evidence_level IN ('insufficient','simulated','observed')),
    basis TEXT NOT NULL, assigned_at TEXT NOT NULL
);

-- ---------------------------------------------------------------- website accounts (login)
CREATE TABLE IF NOT EXISTS users (
    user_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name    TEXT NOT NULL,
    password_hash   TEXT NOT NULL,                 -- scrypt hash with a random salt; the password itself is never stored
    role            TEXT NOT NULL CHECK (role IN ('admin','staff')),
    is_active       INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until    TEXT,
    created_at      TEXT NOT NULL,
    last_login_at   TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,                   -- SHA-256 of the cookie token; the token itself is never stored
    user_id    INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT NOT NULL,
    username TEXT,
    event    TEXT NOT NULL,                        -- login_ok, login_failed, locked, logout, user_created, ...
    detail   TEXT
);

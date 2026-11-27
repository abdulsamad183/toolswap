CREATE TABLE accounts (
    account_id TEXT PRIMARY KEY CHECK (length(account_id) > 0),
    status TEXT NOT NULL CHECK (status IN ('active', 'suspended')),
    verified INTEGER NOT NULL CHECK (verified IN (0, 1))
) STRICT;

CREATE TABLE cases (
    case_id TEXT PRIMARY KEY CHECK (length(case_id) > 0),
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    issue TEXT NOT NULL CHECK (length(issue) > 0),
    status TEXT NOT NULL CHECK (status IN ('open', 'closed')),
    notes TEXT NOT NULL,
    replacement_approved INTEGER NOT NULL CHECK (replacement_approved IN (0, 1))
) STRICT;

CREATE TABLE inventory (
    sku TEXT NOT NULL CHECK (length(sku) > 0),
    location TEXT NOT NULL CHECK (length(location) > 0),
    available_count INTEGER NOT NULL CHECK (available_count >= 0),
    reserved_count INTEGER NOT NULL CHECK (reserved_count >= 0),
    PRIMARY KEY (sku, location)
) STRICT;

CREATE TABLE work_orders (
    work_order_id TEXT PRIMARY KEY CHECK (length(work_order_id) > 0),
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    action TEXT NOT NULL CHECK (length(action) > 0),
    status TEXT NOT NULL CHECK (status IN ('created', 'completed', 'cancelled')),
    request_key TEXT NOT NULL UNIQUE CHECK (length(request_key) > 0)
) STRICT;

CREATE TABLE notifications (
    message_id TEXT PRIMARY KEY CHECK (length(message_id) > 0),
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    channel TEXT NOT NULL CHECK (length(channel) > 0),
    status TEXT NOT NULL CHECK (status IN ('queued', 'sent', 'delivered', 'failed')),
    request_key TEXT NOT NULL UNIQUE CHECK (length(request_key) > 0)
) STRICT;

CREATE TABLE state_snapshots (
    state_hash TEXT PRIMARY KEY,
    payload BLOB NOT NULL
) STRICT;

CREATE TABLE run_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    scenario_id TEXT NOT NULL,
    scenario_version TEXT NOT NULL,
    initial_snapshot TEXT NOT NULL REFERENCES state_snapshots(state_hash),
    seed TEXT NOT NULL
) STRICT;

CREATE TABLE effect_ledger (
    sequence INTEGER PRIMARY KEY CHECK (sequence > 0),
    effect_id TEXT NOT NULL UNIQUE,
    tool_id TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    request_key TEXT NOT NULL CHECK (length(request_key) > 0),
    status TEXT NOT NULL CHECK (status IN ('not_run', 'partial', 'complete', 'duplicate')),
    state_before_hash TEXT NOT NULL REFERENCES state_snapshots(state_hash),
    state_after_hash TEXT NOT NULL REFERENCES state_snapshots(state_hash),
    result BLOB NOT NULL,
    CHECK (status NOT IN ('not_run', 'duplicate') OR state_before_hash = state_after_hash),
    CHECK (status != 'partial' OR state_before_hash != state_after_hash)
) STRICT;

CREATE TABLE idempotency_keys (
    request_key TEXT PRIMARY KEY CHECK (length(request_key) > 0),
    fingerprint TEXT NOT NULL,
    effect_id TEXT NOT NULL REFERENCES effect_ledger(effect_id)
) STRICT;

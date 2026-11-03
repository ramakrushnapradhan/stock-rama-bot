CREATE TABLE IF NOT EXISTS trades (
  trade_id TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  updated_at INTEGER,
  buy_tag TEXT NOT NULL UNIQUE,
  buy_order_id TEXT,
  filled_qty INTEGER NOT NULL DEFAULT 0,
  gtt_id TEXT,
  gtt_status TEXT,
  last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_status_created
  ON trades(status, created_at);

CREATE TABLE IF NOT EXISTS processed_updates (
  update_id TEXT PRIMARY KEY,
  processed_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

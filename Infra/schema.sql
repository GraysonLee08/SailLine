-- infra/schema.sql (initial)
CREATE TABLE IF NOT EXISTS user_profiles (
  id              TEXT PRIMARY KEY,
  tier            TEXT NOT NULL DEFAULT 'free',
  stripe_id       TEXT UNIQUE,
  boat_class      TEXT,
  handicap_system TEXT,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);
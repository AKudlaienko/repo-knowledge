-- 002_decisions_author_supersede.sql — decisions override gate.
--
-- Adds author attribution + a structured supersede link to the decisions
-- table so shared-PostgreSQL teammates can see who set each standard and,
-- when one decision overrides another, why.
--
--   * author          — git identity (UNIX-login fallback), stamped on every
--                        decision. NULL on rows recorded before this migration.
--   * supersedes      — id of the decision this one overrides (NULL otherwise).
--   * override_reason — the required justification comment for an override.
--
-- Mirrors the additive backfill in knowledge/db.py init_schema(). Nullable
-- and backward-compatible — no SCHEMA_VERSION bump, no re-index.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS. Re-running is a no-op.

ALTER TABLE decisions ADD COLUMN IF NOT EXISTS author          TEXT;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS supersedes      BIGINT;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS override_reason TEXT;

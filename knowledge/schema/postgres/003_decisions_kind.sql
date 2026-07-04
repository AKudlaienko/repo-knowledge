-- 003_decisions_kind.sql — facts live in the decisions store.
--
-- Adds a `kind` discriminator to `decisions` so a "working fix / research
-- finding" (kind='fact') can live in the same table+embedding index as a
-- "choice among alternatives" (kind='decision') — no new table, no new
-- retrieval path. `knowledge fact` is a thin wrapper over the `decide`
-- plumbing (author stamping, outbox buffering, supersede gating all
-- inherited); `knowledge decisions --search` covers both kinds by default so
-- the mandated pre-change conflict check sees facts too.
--
-- Mirrors the additive backfill in knowledge/db.py init_schema() (SQLite) and
-- the project_variables.source precedent below. Nullable-free: a NOT NULL
-- DEFAULT keeps every pre-existing row valid without a backfill UPDATE.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS. Re-running is a no-op.

ALTER TABLE decisions ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'decision';

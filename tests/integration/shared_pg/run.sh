#!/usr/bin/env bash
#
# Shared-PostgreSQL teammate integration test.
#
# Brings up an isolated pgvector container, simulates two teammates (Alice &
# Bob) as host processes with their own HOME + git identity, and exercises the
# full shared-mode story end to end against the real `knowledge` CLI:
#
#   S1  migrate a project SQLite -> shared PG (keeping the SQLite copy)
#   S2  an architecture change -> a recorded decision (author = Alice)
#   S3  decision overwrite + the override gate (nudge / exit-3 / supersede=Bob)
#   S4  "stuck user" / no-locks: a held advisory lock never blocks decide+reads,
#       only a competing build/update fails fast (exit 3)
#   S5  connection drop -> offline outbox buffer (file-backed, drains on
#       reconnect with "synced N"); a read while down exits 4 cleanly
#
# The 1-2 GB "buffer cap" the original request imagined does not exist and is
# not needed: the outbox is file-on-disk, fsync'd per entry, unbounded — so an
# in-memory overflow is impossible by construction. S5 verifies the real,
# file-backed behavior instead. See tests/README.md.
#
# Requirements: Docker + compose, and the `knowledge` CLI on PATH with the
# [postgres] extra installed. Nothing here touches your real ~/.knowledge.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PGUSER="${KNOWLEDGE_PG_USER:-itest}"
PGPASS="${KNOWLEDGE_PG_PASSWORD:-itestpw}"
PGPORT="${ITEST_PG_PORT:-5433}"
PG_CONTAINER="knowledge-pg-itest"
LOCK_NS=$((0x6B6E6F77))   # _LOCK_NAMESPACE from knowledge/backends/postgres.py

COMPOSE=(docker compose -p knowledge-itest -f "$SCRIPT_DIR/docker-compose.yml")
HOST_MODELS="$HOME/.knowledge/models"   # captured before any HOME override

export KNOWLEDGE_PG_USER="$PGUSER" KNOWLEDGE_PG_PASSWORD="$PGPASS" ITEST_PG_PORT="$PGPORT"

WORK=""
cleanup() {
  local rc=$?
  step "Teardown"
  "${COMPOSE[@]}" down -v >/dev/null 2>&1 || true
  [ -n "$WORK" ] && rm -rf "$WORK" || true
  log "removed container + temp workdir"
  exit $rc
}
trap cleanup EXIT INT TERM

# Run a knowledge command as a given user (own HOME + git identity). Storage
# routing is by the fixture's .knowledge-config.json (mode resolves from cwd),
# NOT by env — KNOWLEDGE_DATABASE_URL does not flip storage.mode. cwd is the
# fixture repo (set by the caller); creds come from the exported PG env vars.
as_pg()     { local home="$1"; shift; HOME="$home" "$@"; }
# Same identity, but force local SQLite via the --local-sqlite flag at the call
# site (used for the pre-migrate build and the "still in sqlite" assertion).
as_sqlite() { local home="$1"; shift; HOME="$home" "$@"; }

psql_c() { docker exec -e PGPASSWORD="$PGPASS" "$PG_CONTAINER" psql -U "$PGUSER" -d knowledge -tAc "$1"; }

pg_wait() {
  local i
  for i in $(seq 1 30); do
    if docker exec "$PG_CONTAINER" pg_isready -U "$PGUSER" -d knowledge >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  echo "postgres did not become ready" >&2; return 1
}

# Drop a project-scoped config pointing the fixture at the shared PG. Storage
# mode is resolved from this file (walked up from cwd), credentials from env.
write_pg_config() {
  cat > "$FIXTURE/.knowledge-config.json" <<EOF
{
  "storage": {
    "mode": "shared_postgresql",
    "postgresql": {
      "host": "127.0.0.1",
      "port": ${PGPORT},
      "database": "knowledge",
      "sslmode": "disable",
      "user_env": "KNOWLEDGE_PG_USER",
      "password_env": "KNOWLEDGE_PG_PASSWORD",
      "connect_timeout_seconds": 5
    }
  }
}
EOF
}

# ---------------------------------------------------------------------------
step "S0  Preflight & bring-up"
command -v docker >/dev/null || { echo "docker not found" >&2; exit 2; }
command -v knowledge >/dev/null || { echo "knowledge CLI not found on PATH" >&2; exit 2; }
knowledge config check-env >/dev/null 2>&1 || true
[ -d "$HOST_MODELS" ] || log "WARN: $HOST_MODELS absent — first build will download ~130MB"

log "building + starting isolated pgvector ($PG_CONTAINER on :$PGPORT)"
"${COMPOSE[@]}" up -d --build >/dev/null
pg_wait
pass "postgres up and accepting connections"

# Fixture repo + two user homes -------------------------------------------
WORK="$(mktemp -d -t knowledge-itest.XXXXXX)"
FIXTURE="$WORK/sample-service"
ALICE="$WORK/home-alice"
BOB="$WORK/home-bob"
mkdir -p "$FIXTURE" "$ALICE" "$BOB"

cat > "$ALICE/.gitconfig" <<'EOF'
[user]
	name = Alice Dev
	email = alice@example.com
EOF
cat > "$BOB/.gitconfig" <<'EOF'
[user]
	name = Bob Ops
	email = bob@example.com
EOF

# Share the embedding model cache so neither user re-downloads it.
for h in "$ALICE" "$BOB"; do
  mkdir -p "$h/.knowledge"
  [ -d "$HOST_MODELS" ] && ln -s "$HOST_MODELS" "$h/.knowledge/models"
done

# A small, real-looking project so build/migrate/relations have something to chew.
cat > "$FIXTURE/app.py" <<'EOF'
"""Tiny sample service used by the integration test."""
from util import normalize


def handle(request: dict) -> dict:
    return {"ok": True, "path": normalize(request.get("path", "/"))}
EOF
cat > "$FIXTURE/util.py" <<'EOF'
def normalize(path: str) -> str:
    return "/" + path.strip("/")
EOF
cat > "$FIXTURE/README.md" <<'EOF'
# sample-service
Fixture project for the knowledge shared-PG integration test.
EOF
git -C "$FIXTURE" init -q
git -C "$FIXTURE" -c user.name="Alice Dev" -c user.email="alice@example.com" add -A
git -C "$FIXTURE" -c user.name="Alice Dev" -c user.email="alice@example.com" commit -qm "initial sample-service"
pass "fixture repo + Alice/Bob homes ready ($FIXTURE)"

cd "$FIXTURE"

# ---------------------------------------------------------------------------
step "S1  Migrate SQLite -> shared PG (keep SQLite copy)"
log "Alice builds the fixture into local SQLite"
as_sqlite "$ALICE" knowledge build >/dev/null
expect_exit 0 "Alice sees fixture in local SQLite" -- \
  as_sqlite "$ALICE" knowledge projects --local-sqlite
assert_contains "fixture present in SQLite" "sample-service"

log "point the fixture at the shared PG (.knowledge-config.json) and apply schema"
write_pg_config
expect_exit 0 "db init-postgres (idempotent)" -- \
  as_pg "$ALICE" knowledge db init-postgres

log "migrate the project into the shared PG"
expect_exit 0 "db migrate --yes succeeds" -- \
  as_pg "$ALICE" knowledge db migrate --project "$FIXTURE" --yes
expect_exit 0 "fixture now listed in PG" -- \
  as_pg "$ALICE" knowledge projects
assert_contains "fixture present in PG" "sample-service"
# Not deleted from sqlite (we never ran forget --sqlite-only):
expect_exit 0 "fixture STILL in local SQLite after migrate" -- \
  as_sqlite "$ALICE" knowledge projects --local-sqlite
assert_contains "sqlite copy retained" "sample-service"

PID="$(psql_c "SELECT id FROM projects ORDER BY id LIMIT 1")"
[ -n "$PID" ] && pass "resolved PG project id=$PID" || fail "could not resolve PG project id"

# ---------------------------------------------------------------------------
step "S1b  KNOWLEDGE_DATABASE_URL alone selects PG (no config file)"
# Run from $WORK, which has NO discoverable .knowledge-config.json, so the only
# thing that can pick PostgreSQL is the env DSN itself (creds inline).
DB_URL="postgresql://${PGUSER}:${PGPASS}@127.0.0.1:${PGPORT}/knowledge"
expect_exit 0 "db ping via URL only (no config file)" -- \
  bash -c "cd '$WORK' && HOME='$ALICE' KNOWLEDGE_DATABASE_URL='$DB_URL' knowledge db ping"
# The PG ping reports 'pgvector:' + a role; the SQLite ping says 'connected to
# sqlite'. Asserting the former proves the env URL flipped the backend.
assert_contains "ping reached PostgreSQL (not SQLite)" "pgvector:"

# ---------------------------------------------------------------------------
step "S2  Architecture change -> recorded decision (author = Alice)"
# Simulate an architecture change in the working tree.
cat >> "$FIXTURE/util.py" <<'EOF'


def normalize_v2(path: str) -> str:
    # arch change: collapse duplicate slashes too
    import re
    return "/" + re.sub(r"/+", "/", path).strip("/")
EOF
expect_exit 0 "Alice records an architecture decision" -- \
  as_pg "$ALICE" knowledge decide "path-normalization" \
    --decision "Adopt normalize_v2 (collapses duplicate slashes)" \
    --rationale "old normalize left // in paths" \
    --files util.py
AID="$(printf '%s' "$LAST_OUT" | sed -n 's/.*decision id=\([0-9]*\).*/\1/p' | head -1)"
[ -n "$AID" ] && pass "captured Alice decision id=$AID" || fail "could not parse decision id" "$LAST_OUT"
assert_contains "decision stamped with Alice's identity" "Alice Dev <alice@example.com>"

# Confirm it landed in PG with the right author.
A_AUTHOR="$(psql_c "SELECT author FROM decisions WHERE id=${AID}")"
assert_eq "PG decision author = Alice" "Alice Dev <alice@example.com>" "$A_AUTHOR"

# ---------------------------------------------------------------------------
step "S3  Decision overwrite + override gate"
# (a) Bob writes the SAME topic without --supersede: allowed, but nudged.
expect_exit 0 "Bob plain same-topic decide is allowed" -- \
  as_pg "$BOB" knowledge decide "path-normalization" \
    --decision "Keep normalize_v2 but document the regex" --files util.py
assert_contains "non-blocking supersede nudge shown" "supersede"

# (b) Bob tries to override WITHOUT a reason -> hard block, exit 3.
expect_exit 3 "override without --override-reason is blocked (exit 3)" -- \
  as_pg "$BOB" knowledge decide "path-normalization" \
    --decision "Revert to normalize" --supersede "$AID"

# (c) Bob overrides correctly -> exit 0, attributed to Bob, links to Alice's id.
expect_exit 0 "Bob overrides with --supersede + --override-reason" -- \
  as_pg "$BOB" knowledge decide "path-normalization" \
    --decision "Revert to normalize; normalize_v2 regressed encoded slashes" \
    --supersede "$AID" \
    --override-reason "normalize_v2 broke %2F handling in prod"
BID="$(printf '%s' "$LAST_OUT" | sed -n 's/.*decision id=\([0-9]*\).*/\1/p' | head -1)"
OVR_JSON="$(psql_c "SELECT author||'|'||COALESCE(supersedes::text,'') FROM decisions WHERE id=${BID}")"
assert_eq "override author = Bob & supersedes = Alice's id" \
  "Bob Ops <bob@example.com>|${AID}" "$OVR_JSON"

# ---------------------------------------------------------------------------
step "S4  No-locks: a stuck user never blocks decide/reads"
log "holding the project's advisory lock in a separate PG session for 12s"
docker exec -e PGPASSWORD="$PGPASS" "$PG_CONTAINER" \
  psql -U "$PGUSER" -d knowledge -c \
  "BEGIN; SELECT pg_advisory_xact_lock(${LOCK_NS}, ${PID}); SELECT pg_sleep(12); COMMIT;" \
  >/dev/null 2>&1 &
LOCK_BG=$!
sleep 2   # ensure the lock is acquired

# decide takes NO lock -> must succeed immediately even while the lock is held.
expect_exit 0 "decide is NOT blocked by the held lock" -- \
  as_pg "$BOB" knowledge decide "lock-probe" --decision "writes must not block on a busy indexer"
# reads take NO lock either.
expect_exit 0 "read (decisions) is NOT blocked by the held lock" -- \
  as_pg "$BOB" knowledge decisions --limit 1
# a competing build/update on the SAME project must FAIL FAST (exit 3), not hang.
expect_exit 3 "competing update fails fast with exit 3 (no deadlock)" -- \
  portable_timeout 10 env HOME="$BOB" knowledge update
wait "$LOCK_BG" 2>/dev/null || true
log "advisory lock released"

# ---------------------------------------------------------------------------
step "S5  Connection drop -> offline outbox buffer"
DECS_BEFORE="$(psql_c "SELECT count(*) FROM decisions")"
log "stopping postgres (simulates a dropped/refused connection)"
"${COMPOSE[@]}" stop pg >/dev/null

N=12
log "Alice records $N decisions + 2 history entries while PG is DOWN"
for i in $(seq 1 "$N"); do
  expect_exit 0 "buffered decide #$i exits cleanly" -- \
    as_pg "$ALICE" knowledge decide "offline-note-$i" --decision "buffered while PG down ($i)"
done
expect_exit 0 "buffered history add #1 exits cleanly" -- \
  as_pg "$ALICE" knowledge history add --short "offline work 1" --long "buffered detail 1"
expect_exit 0 "buffered history add #2 exits cleanly" -- \
  as_pg "$ALICE" knowledge history add --short "offline work 2" --long "buffered detail 2"

# A READ while down must exit 4 cleanly (no traceback).
expect_exit 4 "read while PG down exits 4 (shared index unreachable)" -- \
  as_pg "$ALICE" knowledge decisions --limit 1
if printf '%s' "$LAST_OUT" | grep -qi "Traceback"; then
  fail "offline read leaked a traceback" "${LAST_OUT:0:300}"
else
  pass "no Python traceback on offline read"
fi

# The buffer is a real file on disk (fsync'd), one JSON line per entry.
OUTBOX="$(find "$ALICE/.knowledge/stage" -name outbox.jsonl 2>/dev/null | head -1)"
if [ -n "$OUTBOX" ] && [ -f "$OUTBOX" ]; then
  LINES="$(wc -l < "$OUTBOX" | tr -d ' ')"
  assert_eq "outbox holds all $((N+2)) buffered entries on disk" "$((N+2))" "$LINES"
  log "outbox: $OUTBOX ($LINES lines, $(wc -c < "$OUTBOX" | tr -d ' ') bytes — file-backed, no memory growth)"
else
  fail "outbox.jsonl not created under Alice's stage dir"
fi

log "restarting postgres; next reachable command should auto-drain"
"${COMPOSE[@]}" start pg >/dev/null
pg_wait
expect_exit 0 "reconnect + auto-drain via knowledge update" -- \
  as_pg "$ALICE" knowledge update
assert_contains "drain reports synced entries" "synced"

PENDING="$(as_pg "$ALICE" knowledge update >/dev/null 2>&1; \
           find "$ALICE/.knowledge/stage" -name outbox.jsonl -size +0c 2>/dev/null | wc -l | tr -d ' ')"
assert_eq "outbox empty after drain" "0" "$PENDING"
DECS_AFTER="$(psql_c "SELECT count(*) FROM decisions")"
if [ "$DECS_AFTER" -ge "$((DECS_BEFORE + N))" ]; then
  pass "buffered decisions landed in PG ($DECS_BEFORE -> $DECS_AFTER)"
else
  fail "expected >= $((DECS_BEFORE + N)) decisions in PG, found $DECS_AFTER"
fi

# ---------------------------------------------------------------------------
summary

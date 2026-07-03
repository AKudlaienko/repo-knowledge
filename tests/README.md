# Tests

## Shared-PostgreSQL integration test (`tests/integration/shared_pg/`)

An opt-in, Docker-backed end-to-end test of **team mode**. It stands up an
isolated `pgvector` container and simulates two teammates — **Alice** and
**Bob** — as host processes, each with their own `HOME` and git identity
(so `knowledge` stamps distinct authors). Nothing here touches your real
`~/.knowledge`; every per-user state dir lives under a temp directory.

### What it proves

| Step | Scenario | Assertion |
|------|----------|-----------|
| S1 | Migrate SQLite → shared PG | project appears in PG **and is retained** in SQLite (no `forget`) |
| S2 | Architecture change → `decide` | decision stored in PG, authored by Alice |
| S3 | Decision overwrite + override gate | plain same-topic decide is nudged; `--supersede` **without** a reason → exit 3; with reason → exit 0, authored by Bob, links to Alice's id |
| S4 | "Stuck user" / no locks | a held advisory lock never blocks `decide` or reads; a competing `update` fails fast (**exit 3**, no deadlock) |
| S5 | Connection drop → outbox buffer | writes buffer to disk and exit 0; a read exits 4 cleanly; on reconnect `update` prints `synced N` and the backlog lands in PG |

### Requirements

- Docker + Docker Compose.
- `knowledge` on `PATH`, installed **with the postgres extra**:
  `pip install -e '.[postgres]'`.
- The embedding model cached at `~/.knowledge/models` (symlinked into each
  simulated user so it isn't re-downloaded). The first ever `knowledge build`
  downloads it (~130 MB).

### Run it

```bash
make test-integration
# or directly:
bash tests/integration/shared_pg/run.sh
# or via pytest (skips automatically when Docker is absent):
pytest -m integration tests/integration
```

The container, image, and host port (`5433`) are deliberately distinct from
`make pg-run` (`5432`), so this never collides with a local dev DB. Teardown
(`compose down -v` + temp dir removal) runs even if an assertion fails.

### Note on the "1–2 GB buffer cap"

There is **no** size cap on the offline buffer, and none is needed: the outbox
(`knowledge/outbox.py`) appends each unsent `decide` / `history add` as one
`fsync`'d JSON line under `~/.knowledge/stage/<slug>/outbox.jsonl` and drains it
on the next reachable command. It is file-backed from the first byte, so an
in-memory overflow is impossible by construction. S5 verifies that real,
file-backed behavior rather than a threshold that doesn't exist.

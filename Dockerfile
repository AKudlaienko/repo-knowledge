# repo-knowledge — local development PostgreSQL with pgvector pre-loaded.
#
# Wraps the official pgvector image and bakes the repo's schema files into
# the postgres init directory so the database is fully initialized on
# first boot. Used by the Makefile `pg-run` target to give developers a
# one-command shared_postgresql bring-up.
#
# Two ways to evolve the schema after first boot (data already present):
#   1. `knowledge db init-postgres` from the laptop — every CREATE in the
#      shipped migrations is IF NOT EXISTS, so re-applying is a safe no-op.
#   2. `make pg-clean && make pg-run` — destroys the data volume and
#      re-runs initdb against the new copy. Lose all indexed data.
#
# Pin to a specific pg version (pg17) but accept any pgvector patch level
# for it. Bump the major when we want a new postgres release; that's a
# breaking change for the data volume so it should be deliberate.
FROM pgvector/pgvector:pg17

# Postgres official image runs every *.sql in /docker-entrypoint-initdb.d/
# in lexical order on first initdb (empty data dir). The schema files in
# this repo are numbered (001_init.sql, ...) so order is deterministic.
COPY knowledge/schema/postgres/*.sql /docker-entrypoint-initdb.d/

LABEL org.opencontainers.image.source="https://github.com/AKudlaienko/repo-knowledge"
LABEL org.opencontainers.image.description="repo-knowledge dev PostgreSQL (pgvector) with schema preloaded"

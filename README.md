# repo-knowledge

Local semantic code search. Default storage is a single SQLite DB at `~/.knowledge/index.sqlite` holding chunks + embeddings for many repos. Optional **shared PostgreSQL mode** routes a project (or all of them) to a team-shared pgvector database — opt in per repo with a `.knowledge.yaml`. Respects `.gitignore`, scrubs a short list of secret patterns, no external services beyond the DB you point it at.

Answers *meaning* questions: "how does vault auth work", "where is the ingress load balancer defined", "find the function that handles cert regeneration".

## Install

```bash
git clone <repo-url> ~/git/repo-knowledge
cd ~/git/repo-knowledge
pip install -e .
```

This registers the `knowledge` command globally (or in your active venv). First run downloads `BAAI/bge-small-en-v1.5` (~130MB) to `~/.knowledge/models/`. Torch wheel on macOS ARM is ~300MB — expected.

## Usage (per repo)

```bash
cd ~/git/my-repo
knowledge build          # first time: scan + chunk + embed (cold: 1-5 min)
knowledge update         # incremental; auto-detects changed files
knowledge search "how does the vault callback inject secrets"
knowledge search "terraform resource: load balancer" --kind resource --lang hcl
```

Add more repos the same way — each `knowledge build` registers a new project row in the shared DB. Searches default to the current repo (detected via `git rev-parse --show-toplevel`); `--all-projects` widens.

## Skill integration

Wire the `/knowledge` skill into a project (or into your whole user profile) with a single command:

```bash
cd ~/your-project
knowledge install-skill              # project-scoped → .claude/skills/knowledge/SKILL.md
knowledge install-skill --user       # user-scoped   → ~/.claude/skills/knowledge/SKILL.md
knowledge install-skill --symlink    # symlink to the source (auto-updates on `git pull` in repo-knowledge)
knowledge install-skill --force      # overwrite an existing install
```

The skill auto-builds the index on first use, auto-updates when files have changed, and stores/retrieves per-project work summaries so a new session can pick up where the last one left off (see `knowledge history --help`).

### Auto-flush staged summaries (optional)

To have Claude Code automatically run `knowledge history ingest` at compaction and session end — so staged work summaries always make it into SQLite before the context is summarized away — register the hooks:

```bash
cd ~/your-project
knowledge install-hooks              # → <cwd>/.claude/settings.json  (project-scoped)
knowledge install-hooks --user       # → ~/.claude/settings.json      (every session, any project)
```

The command idempotently merges into an existing `settings.json`; other hooks and config keys are preserved. It registers three events:

- `Stop` — fires after every assistant turn. Incrementally drains the stage so SQLite stays nearly live with the session. If the terminal gets killed abruptly, the previous turn's entries are already persisted.
- `PreCompact` — fires before manual `/compact` or auto-compaction. Catches anything written after the last `Stop`.
- `SessionEnd` — fires when the session closes gracefully. Final sweep.

All three run `knowledge history ingest`. An empty stage is a no-op — user-scoped hooks are safe to install globally; they won't create project rows for repos that don't use `knowledge`.

#### PATH caveat — hooks run in a subshell that may not see your venv

Claude Code runs hook commands in a subprocess. That subprocess inherits `PATH` from whatever launched Claude Code:

- **Terminal launch**: inherits your shell's `PATH`, so a `knowledge` on `PATH` works.
- **GUI/dock launch** (macOS), **launchd service**, **IDE plugin**: often gets a minimal system `PATH` that does NOT include per-user venv directories (e.g. `~/venvs/*/bin`).

If `knowledge` lives in a venv (`which knowledge` points at something like `/Users/you/venvs/claude/bin/knowledge`), the hook silently fails on GUI launches — your stage file stays unflushed.

**Two fixes, pick one:**

1. **Install with `--absolute`** (recommended when the tool lives in a venv):
   ```bash
   knowledge install-hooks --absolute              # project-scoped
   knowledge install-hooks --user --absolute       # user-scoped
   ```
   The hook command is written as an absolute path (e.g. `/Users/you/venvs/claude/bin/knowledge history ingest`), so `PATH` doesn't matter. Re-running `install-hooks` upgrades the existing entry in place — no duplicates.

   Trade-off: the settings.json becomes machine-specific. For a `--user` install that's fine (it's already under `~/.claude/`). For a project-scoped install you want to commit, prefer option 2.

2. **Put `knowledge` on a system `PATH` directory** (portable across teammates):
   ```bash
   sudo ln -s "$(which knowledge)" /usr/local/bin/knowledge
   ```
   Then leave `install-hooks` in its default (bare) mode, so `.claude/settings.json` stays portable.

You can switch modes any time — re-run `install-hooks` with or without `--absolute`; the in-place upgrade rewrites existing entries cleanly.

**Verify the flow:**
1. Run `knowledge history stage --short "..." --long "..."` during a Claude Code session. This appends to `~/.knowledge/stage/<project-slug>/sess-<session-id>.jsonl` — isolated per project and per session so concurrent Claude instances can't clobber each other's staged work.
2. Run `/compact` (or let auto-compact fire).
3. `knowledge history recent --limit 1` — the entry should be in the DB and the per-session stage file gone (ingest deletes it after a successful flush).

## What's indexed

Everything matching `.gitignore` rules is skipped. Supported languages: Python, JavaScript/TypeScript, Terraform/HCL, YAML (Ansible + Helm + K8s manifests), JSON, Shell, Jinja2, Dockerfile, Markdown.

The following domains also get a **file-to-file dependency graph** (`knowledge relations <file>`): Python, JavaScript/TypeScript, Terraform/HCL (module sources + templatefile/file), Helm (Chart.yaml deps + intra-chart `{{ include }}`), Ansible (include_tasks/import_tasks, include_role/import_role honoring `ansible.cfg` `roles_path`, custom modules in `library/`/`action_plugins/`), GitHub Actions (local reusable workflows + composite actions, external `uses:` passed through), and Kustomize (`resources`, `bases`, `components`, patches, generators). Before opening code to answer a question, you can ask the graph which files are worth reading first — compact JSON designed for LLM consumption.

**Dynamic paths** like `include_tasks: "_tasks/{{ deploy_env }}/..."` or Terraform `source = "./${var.env}"` can be resolved by setting **per-project variables**: `knowledge vars set ansible deploy_env=prod` (or `knowledge vars import ansible vars.json` for bulk). Scoped by domain (`ansible`/`terraform`/`helm`/`all`); auto-applies against existing edges. Edges waiting for variables show as `kind="parametric"` (distinct from `external` stdlib or `unresolved` non-literal expressions).

**Visualize as HTML**: `knowledge graph [--output file.html] [--open]` writes a self-contained HTML (vis-network via CDN) with nodes colored by top-level directory and hover tooltips showing full paths. Resolved project-to-project edges by default; flags add `--include-external` / `--include-parametric` / `--include-unresolved`.

## Secret sanitization

Two layers applied before any chunk is embedded:

1. **Regex scrub** — `ghp_*`, `github_pat_*`, `hvs.*`, `AKIA*`, JWTs, `-----BEGIN ... PRIVATE KEY-----`, long SSH keys → `CHANGE_ME`.
2. **Sensitive-key replacement** — in YAML/HCL/JSON, values under keys like `password`, `*_token`, `*_secret`, `api_key`, `vault_*_id` → `CHANGE_ME`.

Plus `.gitignore` + `.knowledgeignore` are honored, so gitignored files (where secrets usually live) aren't scanned at all.

## Layout

See `knowledge/README.md` for the module mapping table.

## Shared PostgreSQL mode

Default storage is local SQLite — fine for solo work. Switch a project (or every project on the laptop) to a team-shared **pgvector** database when you want teammates to share the same index, history, and decisions.

**Storage choice is per project.** The same machine can keep project A on shared PG and project B on local SQLite. Resolution at runtime:

1. `KNOWLEDGE_DATABASE_URL` env (CI override) — full DSN, wins everything
2. Walk cwd → cwd's parents looking for `.knowledge.yaml` — first match wins
3. `$HOME/.knowledge.yaml` — laptop-wide default
4. Built-in default: SQLite

The same file name and schema at every scope ([template](knowledge/config.example.yaml)). The closer file wins.

### Quick start (Docker dev container)

```bash
pip install -e '.[postgres]'                       # install psycopg + pgvector

export KNOWLEDGE_PG_USER={your-user}
export KNOWLEDGE_PG_PASSWORD=$(openssl rand -hex 16)
make pg-run                                        # builds image, starts container, applies schema

cd /path/to/your-repo
knowledge config init --project                    # writes ./.knowledge.yaml from template
$EDITOR .knowledge.yaml                            # mode=shared_postgresql, host=127.0.0.1, sslmode=disable

knowledge config show                              # confirms which file is active + masked DSN
knowledge db ping                                  # opens the DB, prints version + extension status

knowledge db init-postgres                         # idempotent re-apply of the schema
knowledge build                                    # first build greenfield on PG
knowledge ask "..."                                # all verbs route to PG from this cwd
```

Alternative: drop the `.knowledge.yaml` at `$HOME` to make PG the laptop default for every project that doesn't override.

### Migrating an existing SQLite project

The local SQLite copy stays untouched — `migrate` only writes to the target.

```bash
knowledge db migrate --project <name|abs-path> --dry-run    # see the plan
knowledge db migrate --project <name|abs-path>              # interactive confirm
knowledge db migrate --project <name|abs-path> --yes        # scripts/CI

knowledge forget <name> --sqlite-only                       # drop the local copy after verifying the PG one
```

`migrate` keys on the project's `git remote` URL (normalized: strip credentials, drop `.git`, lowercase host, ssh→https) so the same repo cloned at different paths on different laptops collapses to one row on PG. Falls back to `root_path` when there's no `.git`.

### Credentials

Never in `.knowledge.yaml`. Each laptop exports its own `KNOWLEDGE_PG_USER` / `KNOWLEDGE_PG_PASSWORD` ([template](knowledge/config.example.env)). The YAML carries env-var **names** only — committed configs can't leak secrets even if checked in.

`KNOWLEDGE_DATABASE_URL` is the CI escape hatch (full libpq URL with credentials inline). Don't use it on laptops.

### Plan + design notes

`todo/01-postgresql-shared-mode.md` — schema, ID-remap, advisory lock strategy, identity rules.

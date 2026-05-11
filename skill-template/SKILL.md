---
name: knowledge
description: Local code cartography + semantic search for the current repo. Navigate (find / grep), orient (why / map / brief), search (ask — hybrid FTS+vector+rerank, cached), and remember across sessions (decide / resume). Auto-builds or updates the local index. Use BEFORE raw Grep / Read for anything meaning-shaped.
argument-hint: [verb] [args]
allowed-tools: Bash Read
---

# /knowledge — Code cartography + semantic search + session memory

One SQLite DB at `~/.knowledge/index.sqlite` holds chunks, edges, history, and decisions for every repo the user has indexed. Complements `Grep` (exact text) and `graphify` (one-off structural dependencies).

## Priority directives — READ FIRST

These three rules apply on every invocation. They exist because they reduce tool-call count and keep cross-session continuity intact.

1. **On a new session, run `knowledge resume` BEFORE any other tool** in this skill.  It returns last decisions + touched files + any un-ingested stage entries + hub files. ~1200 tokens, <200ms. Skip only when the user's very first message makes it obvious (e.g. a typo fix on a specific line).

2. **Default to `knowledge ask` instead of `knowledge search`.** `ask` runs FTS + vector in parallel, merges via RRF, reranks by recency/session/hub centrality, caches by (query, HEAD sha). `search` is the vector-only raw-chunks path — use it only when you need `--top-k` with distance scores or downstream scripting.

3. **Log non-obvious choices with `knowledge decide` as you make them, not at session end.** Each decision is embedded — `knowledge resume` surfaces the latest five every new session, and `knowledge decisions --search "<topic>"` finds older ones. A two-minute `decide` call today saves a 20-minute "why did we do this" excavation next week.

## Auto-maintenance — run BEFORE any query verb

```bash
knowledge status --json
```

Branch on `state`:
- `missing` → `knowledge build` (first-time: 1–5 min for embedding model + initial encode; warn the user).
- `stale`   → `knowledge update` (usually <5s; only re-embeds chunks whose sanitized text changed).
- `fresh`   → go straight to your query verb.

## The six agent-speed verbs

These bypass the embedding model entirely (`find`, `grep`) or cache their answers (`ask`). Prefer them over reading raw files for meaning questions.

### `find <name>` — exact/prefix/regex symbol lookup

```bash
knowledge find VaultClient --exact          # SQL equality on name / qualified_name
knowledge find regen --kind ansible_task    # prefix (default), filtered to Ansible tasks
knowledge find '^handle_' --regex           # Python regex — use flags like (?i) for case
```

Under 10ms. Use when you know a symbol name and want its source location.

### `grep <pattern>` — FTS5 full-text match

```bash
knowledge grep 'helm install'
knowledge grep '"exact phrase"'                # phrase search
knowledge grep 'vault AND approle'             # boolean
knowledge grep 'name:VaultClient'              # column qualifier
knowledge grep 'regenerate*' --kind ansible_task
```

Full FTS5 query syntax. Tokens-only index (no embedder). Use for lexical precision.

### `ask <question>` — hybrid semantic + lexical (the default)

```bash
knowledge ask "how does vault auto_load inject secrets"
knowledge ask "octavia LB floating IP" --top-k 5
knowledge ask "cert regen" --budget 2000           # soft token budget for citation list
knowledge ask "<question>" --no-cache              # force fresh (skip 1h cache)
```

RRF merge of vec + FTS, reranked by last-30d git, current session stage, and import-graph hub in-degree. Cached per (query, HEAD sha) with 1h TTL; invalidated in the same txn whenever the indexer mutates a chunk.

### `why <path>` — one-file brief

```bash
knowledge why ansible/roles/karmada/tasks/main.yml
knowledge why python_packages/kickstart/utils.py
```

Returns: lang/loc/last-commit-date, first description line, top 5 symbols by size, top 3 inbound + outbound edges. ~100ms. Use to orient on a file before reading it.

### `map [--dir PATH] [--depth N]` — directory overview

```bash
knowledge map --depth 2                     # whole repo
knowledge map --dir terraform --depth 3     # one subtree
```

Per-dir: file count, dominant language, top 3 non-structural chunk kinds, highest-in-degree "entrypoint" file. Truncates at 200 rows.

### `brief` — repo-wide snapshot

```bash
knowledge brief
```

Totals, top 5 langs, top 10 hub files by in-degree. Run once on unfamiliar repos to build a mental model before asking specific questions.

## Session memory — `decide` + `resume`

See the priority directives above. Full detail:

### `decide` — record a non-obvious choice

```bash
knowledge decide "cache invalidation" \
  --decision "wipe per-project on any chunk change; preserve on no-op update" \
  --rationale "agent-driven updates on every turn shouldn't thrash cache" \
  --files knowledge/query_cache.py knowledge/indexer.py
```

Topic + decision are the keys. Rationale and file list are optional but valuable — the rationale is what future-you actually needs to remember.

### `decisions` — list or semantically search

```bash
knowledge decisions --limit 5
knowledge decisions --topic cache              # substring filter on topic
knowledge decisions --search "how to handle stale caches"   # semantic over topic+decision
```

### `resume` — the session-start brief

```bash
knowledge resume
```

Four blocks in order: last 5 decisions, 10 most-touched files (7d), un-ingested stage entries, top 3 hub files. ~1200 tokens, idempotent. Run first on every new session.

## `search` — raw-chunks flow (legacy / specialist use)

Kept for when you need ranked vector results without RRF/rerank/cache — e.g. comparing distances, piping to downstream code, or debugging retrieval.

```bash
knowledge search "$ENRICHED_QUERY" [--kind K] [--lang L] [--top-k 10]
```

For normal agent use, prefer `ask`.

## Query enrichment — rewrite the user's question before searching

The embedding model retrieves best when the query hints at *what kind of thing* is being sought. Prefix user queries based on their intent:

| User is looking for | Prefix the query with | Good `--kind` filter |
|---|---|---|
| Python function body | `python function:` | `function` or `big_parent` |
| Python class | `python class:` | `class` |
| Python method | `python method:` | `method` (M5 hierarchy, fallback: `function`) |
| JS / TS function | `javascript function:` | `function` |
| Terraform resource | `terraform resource:` | `resource` |
| Terraform variable / output | `terraform variable:` / `terraform output:` | `variable` / `output` |
| Terraform module | `terraform module:` | `module` |
| Terraform locals | `terraform locals:` | `locals_block` or `locals_entry` |
| Ansible task | `ansible task:` | `ansible_task` |
| Ansible handler | `ansible handler:` | `ansible_handler` |
| Helm template | `helm template:` | `helm_template` |
| Helm values key | `helm values:` | `helm_values_section` |
| K8s manifest | `kubernetes <Kind>:` (e.g. `kubernetes Deployment:`) | `yaml_doc` + `--lang yaml` |
| Shell function | `shell function:` | `shell_function` |
| Jinja macro or block | `jinja:` | `jinja_macro` / `jinja_block` |
| Dockerfile stage | `dockerfile stage:` | `dockerfile_stage` |
| Markdown doc / README | `docs:` | `markdown_section` |
| Config value / literal | `value:` | (no filter) |
| Docstring / doc comment | `docstring:` | (no filter) |

**Filters narrow the candidate pool AFTER semantic ranking.** If the top-K without a filter already matches, skip the filter. If irrelevant kinds crowd the result, add one.

## Cross-repo mode

By default, `knowledge search` scopes to the current repo (detected via `git rev-parse --show-toplevel`). Use `--all-projects` to search across every registered repo:

```bash
knowledge search "vault auto_load convention" --all-projects
```

Useful when the user's question is about *another* repo they've indexed, or when they want to see how a pattern is used across multiple projects.

## Reading a specific chunk

Each search result includes a `chunk_id`. To see the full contents:

```bash
knowledge get <chunk_id>                       # sanitized stored text
knowledge get <chunk_id> --with-siblings       # for big_parent: parent + all subchunks
knowledge get <chunk_id> --with-siblings --raw # exact original bytes from disk
knowledge path <chunk_id>                      # file_path:start_line-end_line
```

`--raw` re-slices the original file using the chunk's byte offsets — byte-identical to what's on disk. Use this when the user asks to see the actual code (not the sanitized DB copy).

## Rules / gotchas

- **First build is slow** — cold-start downloads the 130MB embedding model to `~/.knowledge/models/`. Warn the user before running `build` on a fresh machine.
- **Don't commit the DB** — `~/.knowledge/index.sqlite` is per-machine. Each teammate rebuilds locally.
- **`.gitignore` is honored.** Secret-shaped files (`.env`, `*.pem`, etc.) that are gitignored are never scanned. Regex + structured-key sanitization scrub the rest. Any `CHANGE_ME` token in search results is either a user placeholder or a sanitizer replacement — never a real leaked secret.
- **Version drift → rebuild.** If the tool's chunker or embedding model was bumped, `update` auto-falls-back to `build` and warns you. Other projects in the shared DB need their own `build` too.
- **`.knowledgeignore`** in the repo root takes gitignore-style patterns for extra exclusions (e.g., generated docs) without polluting `.gitignore`.

## Example end-to-end

User: "how does the karmada cert regeneration ansible task work"

1. New session → `knowledge resume` to load prior context.
2. `knowledge status --json` → `{"state": "fresh", ...}` → no maintenance needed
3. `knowledge ask "karmada cert regeneration" --kind ansible_task --top-k 5`
4. Top result: `ansible/roles/karmada/tasks/main.yml:47-55 | ansible_task | name: Regenerate Karmada TLS certificates`
5. `knowledge why ansible/roles/karmada/tasks/regenerate_certs.yml` for the included file's neighbors.
6. Summarize for the user referencing `ansible/roles/karmada/tasks/main.yml:47`.
7. If this invoked a non-obvious design choice, `knowledge decide "karmada cert rotation approach" --decision "..." --files ansible/roles/karmada/tasks/regenerate_certs.yml`.

## Continuity / memory — cross-session RAG over past work

Two complementary stores:
- **History** (`knowledge history stage|ingest|recent|search`) — free-form work summaries keyed by session/time. Good for "what did we do last Tuesday."
- **Decisions** (`knowledge decide|decisions|resume`) — structured choices with topic/decision/rationale/files. Good for "why did we pick X over Y."

Use **history** for narrative, **decisions** for commitments. `resume` aggregates both plus git and staging state into one session-start brief.

### Session start — check what we did before

When the user opens a new session on a project, or asks a question that sounds historical ("where did we stop", "what did we decide about X", "continue the Y refactor"), consult history **before** doing code search:

```bash
knowledge history recent --limit 10            # newest-first list, no vector work
knowledge history search "auth middleware"     # semantic over short summaries
knowledge history get <id>                     # full entry (short + long)
```

Typical RAG flow: `recent` or `search` → pick the relevant hit(s) → `get <id>` for only the entries you need. Never `get` every recent entry — that defeats the purpose of the two-tier design.

Skip history lookup entirely when the question is clearly about current code ("what does function X do", "find the config for Y") — go straight to `knowledge search`.

### During the session — write staged entries at natural boundaries

At each unit-of-work completion (task done, plan signed off, a focused change shipped), run **one `knowledge history stage` command** per entry:

```bash
knowledge history stage \
  --short "Fixed ambiguous project-name resolution in forget/search." \
  --long  "Added AmbiguousProjectName exception in projects.py. resolve_project now uses fetchall() on the name branch and raises on >1 match. cmd_search and cmd_forget catch and dispatch to _print_ambiguous. Fixes the silent-pick-one behavior.

Files: knowledge/projects.py, knowledge/cli.py.
Decision: keep error-out semantics rather than auto-pick — ambiguity should be user-resolved." \
  --tags "fix,cli,projects"
```

This appends one JSONL line to `~/.knowledge/stage/<project-slug>/sess-<session-id>.jsonl` — isolated per project (via a SHA-1-suffixed slug of the repo root) and per session (via `CLAUDE_SESSION_ID` when present, falling back to `pid<PID>-<epoch>`). You do NOT re-read the file; `knowledge history ingest` handles parsing and DB insert later, which avoids burning tokens on re-reading your own summaries.

Guidelines:
- **Short** ≤ ~160 chars. The imperative-summary bar: someone skimming `recent` should know what happened. One line.
- **Long**: 1–5 paragraphs. Include file paths, rationale, decisions, and non-obvious tradeoffs. Skip obvious things a future session can derive from `git log` or the code.
- **Tags**: optional comma-separated. Useful for filtering (not indexed for semantic search — just metadata).
- Skip trivial Q&A — write entries only when there's something worth recalling later.
- **No secrets.** The sanitizer does not scrub this layer — you control what you write.

### Flushing staged entries to SQLite

Run `knowledge history ingest` when you want to durably persist staged entries:

```bash
knowledge history ingest                                # walks all per-project stage dirs
knowledge history ingest --stage-file /path/to/x.jsonl  # override: flush one file under current project
```

Behavior (default flow):
- Walks every `~/.knowledge/stage/<slug>/` dir and processes each `sess-*.jsonl` file under its own APSW savepoint.
- Each file is atomically renamed to `*.inflight-<pid>-<ts>` before it's read — so three near-simultaneous hook firings (Stop, PreCompact, SessionEnd) can't double-ingest the same file. The winning process deletes the inflight file on commit; any loser skips silently.
- Embeds short summaries in one batch per file and inserts rows transactionally.
- Malformed lines (bad JSON, missing short/long, empty short) are skipped and counted; they do **not** block the valid entries or other files.
- A one-shot migration absorbs the legacy `~/.knowledge/stage/pending.jsonl` (from earlier versions) under the current project, then deletes it.

**When to ingest:** after a batch of entries (e.g. end of a focused work stretch), or before a context-window compact event. A `PreCompact`/`Stop`/`SessionEnd` hook (installed by `knowledge install-hooks`) does this automatically; manual invocation is still fine.

### Rules for history

- **Scope is per-project** by default (uses the current git root). Pass `--all-projects` to `recent`/`search` to cross-project.
- **Don't pollute** with trivial or conversational summaries — the vector index stays useful only if what's in it is worth recalling.
- **Don't search history for code questions** — `knowledge search` is faster and more precise. History is for *decisions, context, and continuity*, not code.

## Dependency graph — first step before code search

`knowledge relations <file>` returns a compact JSON view of file-to-file imports for one file: what it imports (forward edges) and what imports it (reverse edges). **Use this BEFORE `knowledge search`** when the task involves understanding or changing existing code — it tells you which files to pull into context, with no embedding work required.

### When to use

- "How does X work" / "where do I start with file F" — `relations` narrows the search surface before you read anything.
- "What will break if I change Y" — reverse edges list the callers.
- "What does this module depend on" — forward edges with `--kinds import,from_import,require`.
- Skip when the question is clearly semantic ("find the code that authenticates vault tokens") — go straight to `knowledge search`.

### Typical flow

```bash
knowledge relations knowledge/cli.py                                   # both directions, depth 1
knowledge relations knowledge/cli.py --direction forward --depth 2     # follow imports two hops
knowledge relations knowledge/db.py  --direction reverse               # who imports db.py
knowledge relations knowledge/cli.py --kinds import,from_import,require # drop external/unresolved noise
knowledge relations stats                                              # sanity check: edge counts
```

### Output format (LLM-optimized)

Compact JSON by default. One object with `file`, `project`, and the requested direction arrays. Each edge is `{kind, raw, [file], [symbol], [line]}`:

- `kind`: `import` | `from_import` | `require` | `dynamic_import` | `external` | `unresolved`
- `raw`: the literal specifier as written in source (`.db`, `./utils`, `os.path`)
- `file`: project-relative path of the resolved target. **Absent** for external and unresolved edges.
- `symbol`: the imported name for `from_import`, else absent.
- `line`: 1-based source line.

Add `--pretty` for human-readable output when you want to show the user directly.

### Coverage

- **Python**: `import a.b`, `from a import b`, `importlib.import_module('x')`, relative imports.
- **JavaScript/TypeScript**: `import`, `require()`, `import()` dynamic with string-literal arg.
- **Terraform / HCL**: `module "x" { source = "…" }`, `templatefile("…", …)`, `file("…")`. Local relative sources resolve to the module's `main.tf` (fallback to any `.tf` in the dir).
- **Helm**: `Chart.yaml` `dependencies:` (file:// → subchart `Chart.yaml`, remote → external), `{{ include "name" . }}` / `{{ template "name" . }}` → the file in the same chart containing `{{ define "name" }}`. Scope is the containing chart (walk up to the nearest `Chart.yaml`).
- **Ansible**: `import_playbook`, `include_tasks` / `import_tasks`, `include_role` / `import_role` / `roles:`, `vars_files`, `include_vars`. Role resolution honors `ansible.cfg` `roles_path` — multi-cfg / non-root layouts work (e.g. `ansible/ansible.cfg` with `roles_path = roles` resolves to `ansible/roles/`). `tasks_from:` on an include_role narrows the target to the specific task file. Custom modules in `library/` + `action_plugins/` (or wherever `ansible.cfg` points) produce `ansible_module` edges — builtin modules (`debug`, `copy`, …) don't clutter the graph.
- **GitHub Actions**: `uses: ./.github/workflows/*.yml` (reusable workflows), `uses: ./.github/actions/*` (local composite actions → their `action.yml`), `uses: owner/repo@ref` → external.
- **Kustomize**: `kustomization.yaml` `resources`, `bases`, `components`, `patchesStrategicMerge`, `patches[].path`, `configMapGenerator`/`secretGenerator.files`. A `resources:` entry that's a directory is resolved to its nested `kustomization.yaml`. Plain (non-kustomize) k8s manifests have no edges.
- **Plain YAML / Markdown / other files without a resolver**: `knowledge relations <file>` returns the file's same-directory siblings (with lang) under a `siblings` key — a "where does this file live" hint rather than a real dep.

### Freshness

The graph is rebuilt alongside chunks during `knowledge build` / `knowledge update`. Because this skill always runs `knowledge update` before any query (see "Auto-maintenance" at the top), `relations` reflects current-on-disk state.

If `relations` returns `error: file not indexed` for a file you know exists, run `knowledge update` — it's probably new since the last index.

### Variables — resolving `{{ var }}` and `${var.x}` paths

Some edges (mostly Ansible `include_tasks`/`include_role` and Terraform `templatefile`/`source`) carry template expressions like `_tasks/{{ deploy_env }}/...` or `source = "./${var.env}"`. Without the variables, these edges show as `kind="parametric"` with no `file` — the LLM can see *something* is there but not where it points.

Set per-project variables (scoped by domain) to resolve them:

```bash
knowledge vars set ansible deploy_env=prod region=us-east            # multi-kv
knowledge vars set terraform env=prod                                 # scoped separately
knowledge vars set all region=us-east-1                               # catch-all merged into any scope
knowledge vars import ansible /path/to/vars.json                      # bulk from JSON
knowledge vars list [--scope ansible] [--json]
knowledge vars unset ansible deploy_env                               # remove one
knowledge vars unset ansible --all                                    # clear a scope
```

Every mutation auto-applies against the existing graph — no rebuild needed. Scope routing:

| Edge kind | Syntax | Scope lookup order |
|---|---|---|
| `ansible_*` | Jinja `{{ name }}` | `ansible`, then `all` |
| `helm_*` | Jinja `{{ name }}` | `helm`, then `all` |
| `tf_*` | Terraform `${var.name}` | `terraform`, then `all` |

**Display kinds for NULL-target edges:**
- `parametric` — waiting for variables. Set them with `vars set`.
- `external` — resolved to not-a-project-file (stdlib / third-party / remote module source).
- `unresolved` — syntactically irrecoverable (e.g., `import_module(some_expr)` with a non-literal arg).

**Not substituted:** Jinja filters (`{{ x | lower }}` → takes `x`, ignores filter), loop vars (`{{ item }}`, `{{ role_item }}`), nested attrs (`{{ foo.bar }}`), arithmetic/expressions. Those stay parametric by design — set a concrete value if you want them resolved.

### Visualize the graph (HTML)

When the user wants to *see* the dependency shape (e.g. "what's the overall structure here", "show me the graph", "are there cycles"), render it to a static HTML:

```bash
knowledge graph                                    # writes ./relations_graph.html
knowledge graph --output /tmp/graph.html --open    # write to specific path + launch browser
knowledge graph --include-external                 # include stdlib / third-party as gray nodes
knowledge graph --include-parametric               # include vars-waiting as yellow nodes
```

One project per run (`--project` overrides the cwd default). The rendered file is a single self-contained HTML with vis-network loaded from CDN — open in any browser, hover a node for the full project-relative path + language, drag nodes, scroll to zoom. Nodes are colored by top-level directory. The default scope is resolved project-to-project edges only (cleanest for large repos); opt in to `external` / `parametric` / `unresolved` via the flags above.

This is a **display** command, not a query command — it writes a file to disk and prints its path. Don't use it when the user asks a narrow "where does X point" question; `knowledge relations <file>` is faster and more focused for that.

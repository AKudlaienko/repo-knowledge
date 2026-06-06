# AGENTS.md — using `knowledge` in this repo

This repo ships with a local, SQLite-backed file-dependency graph and semantic
index, served by the `knowledge` CLI (upstream: `repo-knowledge`). The DB lives
at `~/.knowledge/index.sqlite` (per-machine, never commit). This file tells any
coding agent — Claude Code, Cursor, Aider, etc. — how to pull the **relations
graph** out of that DB. Search / history are covered briefly at the end.

## 0. Precondition — make sure the index is fresh

Run this before every relations or search query:

```bash
knowledge status --json
```

Branch on the `state` field (exit codes `0/1/2` also work):

| `state`  | do                                                 |
|----------|----------------------------------------------------|
| `fresh`  | skip — proceed to the actual query                 |
| `stale`  | `knowledge update`  (cheap; only re-embeds diffs)  |
| `missing`| `knowledge build`   (first time: 1-5 min; downloads ~130MB embedding model) |

If `knowledge relations <file>` returns `error: file not indexed` for a file
you know exists, the file is new since the last build — run `knowledge update`.

## 1. Pulling relations — the file-to-file dep graph

```bash
knowledge relations <file>                          # both directions, depth 1 (default)
knowledge relations <file> --direction forward      # only what <file> imports
knowledge relations <file> --direction reverse      # only what imports <file>
knowledge relations <file> --depth 2                # transitive, two hops
knowledge relations <file> --kinds import,from_import,require
knowledge relations <file> --project NAME|ABS_PATH  # scope to a specific project
knowledge relations <file> --pretty                 # human-readable JSON (default: compact, LLM-optimal)

knowledge relations stats                           # edge-count summary for current project
knowledge relations stats --all-projects --pretty   # summary across every registered project
```

`<file>` accepts any of: absolute path, path relative to cwd, or path relative
to the project root (posix-style).

### Output shape

Compact JSON (one object). Example:

```json
{
  "file": "knowledge/cli.py",
  "project": "repo-knowledge",
  "forward": [
    {"kind": "external",    "raw": "argparse",                                                  "line": 9},
    {"kind": "from_import", "raw": ".",  "file": "knowledge/db.py",       "symbol": "db",       "line": 25},
    {"kind": "from_import", "raw": ".",  "file": "knowledge/projects.py", "symbol": "projects", "line": 25}
  ],
  "reverse": []
}
```

Per-edge fields:

- **`kind`** — one of `import`, `from_import`, `require`, `dynamic_import`, plus the three display-only kinds below when `file` is absent.
- **`raw`** — the literal specifier as written in source.
- **`file`** — project-relative resolved target. **Absent** for the three cases below.
- **`symbol`** — imported name (`from_import` only).
- **`line`** — 1-based source line.

When `file` is absent, `kind` tells you why:

- **`external`** — resolved past the project (stdlib, third-party, remote module source).
- **`parametric`** — `raw` contains `{{ name }}` / `${var.x}` that isn't satisfied by the current variables table. Set the variable and the edge re-resolves. See section 3.
- **`unresolved`** — resolver couldn't statically determine the target (non-literal `import_module(expr)`, etc.).

### Siblings fallback

If the target file has no edges AND no resolver fired (plain k8s YAML, Markdown,
JSON config), the output carries a `siblings` array instead — the other files
in the same directory. This is a weak "same folder" hint, not a real dep. It's
labeled in the JSON via a `siblings_note` field so agents don't mistake it for
structural data.

## 2. Coverage — what has a graph

| Domain            | What's tracked                                                                                              |
|-------------------|-------------------------------------------------------------------------------------------------------------|
| Python            | `import a.b`, `from a import b`, `importlib.import_module('x')`, relative imports                           |
| JavaScript / TS   | `import`, `require()`, dynamic `import()` with string-literal arg                                           |
| Terraform / HCL   | `module { source = "…" }`, `templatefile("…", …)`, `file("…")`                                               |
| Helm              | `Chart.yaml` deps (file:// → local subchart), `{{ include "name" . }}` / `{{ template "name" . }}` → the `{{ define }}` source in the same chart |
| Ansible           | `import_playbook`, `include_tasks`/`import_tasks`, `include_role`/`import_role`/`roles:`, `vars_files`, `include_vars`, custom modules in `library/`/`action_plugins/` (honors `ansible.cfg` `roles_path`) |
| GitHub Actions    | `uses: ./.github/workflows/*.yml` (reusable), `uses: ./.github/actions/*` (composite), `uses: owner/repo@ref` → external |
| Kustomize         | `resources`, `bases`, `components`, `patchesStrategicMerge`, `patches[].path`, `configMapGenerator`/`secretGenerator.files` |
| Plain YAML / MD / JSON / other | No edges. You get the `siblings` fallback (section 1).                                        |

## 3. Variables — resolving `{{ name }}` and `${var.x}` edges

Some edges (mostly Ansible `include_tasks`/`include_role` and Terraform
`templatefile`/`source`) carry template expressions like
`_tasks/{{ deploy_env }}/…` or `source = "./${var.env}"`. Without the
variables, they show up as `kind="parametric"` with no `file`. Set the
variables and they re-resolve:

```bash
knowledge vars set ansible   deploy_env=prod region=us-east      # multi k=v
knowledge vars set terraform env=prod                             # separate scope
knowledge vars set helm      release=main
knowledge vars set all       region=us-east-1                     # catch-all
knowledge vars import ansible /path/to/vars.json                  # bulk from JSON object
knowledge vars list [--scope ansible] [--json]
knowledge vars unset ansible deploy_env                           # one
knowledge vars unset ansible --all                                # clear a scope
```

Every mutation auto-applies against existing edges — **no rebuild needed**.

Scope lookup order:

| Edge family   | Template syntax    | Scopes checked              |
|---------------|--------------------|-----------------------------|
| `ansible_*`   | `{{ name }}`       | `ansible`, then `all`       |
| `helm_*`      | `{{ name }}`       | `helm`, then `all`          |
| `tf_*`        | `${var.name}`      | `terraform`, then `all`     |

Not substituted (stay parametric by design): Jinja filters (`{{ x | lower }}`
takes `x`, ignores the filter), loop vars (`{{ item }}`, `{{ role_item }}`),
nested attrs (`{{ foo.bar }}`), arithmetic / expressions. Set a concrete value
if you want those resolved.

## 4. Typical agent flow — "understand file F before changing it"

1. `knowledge status --json` → `build` or `update` if needed.
2. `knowledge relations F` → see imports + callers in one JSON blob.
3. Read the files listed under `forward` (what F depends on) and `reverse`
   (who will break if F changes) **before** opening F itself.
4. If the `parametric` / `unresolved` / `siblings` buckets suggest missing
   structural context, fix it with `knowledge vars set …` (section 3) rather
   than falling back to grep.
5. If you still need *semantic* context ("how does vault auth work"), fall
   back to `knowledge search "…"` — see section 6.

## 5. When to use relations vs search vs grep

- **`knowledge relations <file>`** — *structural*. "What does F depend on / who calls F / what will break if I change Y."
- **`knowledge search "<query>"`** — *semantic*. "How does vault auth work / where is the load balancer defined / find the function that handles cert regeneration."
- **`grep` / exact match tooling** — *literal*. Identifier lookups, one-shot string searches, regex on tokens.

Relations first when the task is understanding an existing file. Search first
when the task is finding code by meaning. Grep first when you already know the
exact token.

## 6. Search and history — pointers, not primary

- `knowledge search "<query>" [--kind K] [--lang L] [--top-k N] [--all-projects]` — semantic chunk search. `knowledge get <chunk_id> [--raw] [--with-siblings]` fetches a specific chunk.
- `knowledge history recent|search|get` — per-project work-summary store (cross-session RAG memory). Use before code search when the question sounds historical ("where did we stop", "what did we decide about X").

The full Claude Code skill reference lives at `.claude/skills/knowledge/SKILL.md`
if installed via `knowledge install-skill`.

## 7. Gotchas

- `~/.knowledge/index.sqlite` is per-machine — **do NOT commit**. Each teammate rebuilds locally.
- `.gitignore` and `.knowledgeignore` are honored; gitignored files are never scanned, never embedded, never appear in the graph.
- The sanitizer writes `CHANGE_ME` into stored chunk text for secret-shaped content. `knowledge get <id> --raw` re-slices the original file from disk when the user needs exact bytes. `CHANGE_ME` is never a leaked secret.
- If the chunker / embedding model version was bumped upstream, `knowledge update` auto-falls-back to a full rebuild and prints a warning. Other projects in the shared DB need their own rebuild.
- The SQLite schema is not a public surface — query via the CLI, don't read the tables directly. `SCHEMA_VERSION` can shift between releases.

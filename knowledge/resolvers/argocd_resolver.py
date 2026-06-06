"""ArgoCD resolver — extracts chart references from Application manifests.

Runs alongside ``HelmResolver`` on ``helm_template`` files (see
``resolvers/__init__.py``). ArgoCD's App-of-Apps pattern produces
``Application`` / ``ApplicationSet`` Kubernetes resources whose
``spec.source.path`` / ``spec.sources[*].path`` point at sibling chart
directories in the same repo — a relationship Helm's own dependency
system doesn't see.

Regex-based, not PyYAML-based: the manifests often live inside a
Helm chart's ``templates/`` dir with ``{{ ... }}`` expressions that break
any strict YAML parser. Document boundaries are split on ``^---$`` at
source level; per document we look for a ``kind: Application`` /
``ApplicationSet`` anchor with a matching ``apiVersion: argoproj.io/...``
and then extract every indented ``path:`` found under a ``source:`` /
``sources:`` key.

Edges:

* ``argocd_app_source`` — one per ``path:`` value. ``raw`` is the path as
  written (``charts/foo``), ``symbol`` is the ``metadata.name`` of the
  enclosing Application (useful when multiple Apps in one file each
  target a different chart).

Templated paths (``path: charts/{{ .Values.name }}``) are emitted
verbatim; resolution in ``relations._resolve_argocd`` returns ``None``
and the edge lands parametric. Runtime Helm ``.Values.*`` substitution
is out of scope here — that would need a different system than the
``project_variables`` table used for Ansible/TF/Jinja vars.
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import BaseResolver, Edge


# Document splitter. YAML doc separator is a line containing only ``---``
# (optionally followed by whitespace / a document tag). We do not try to
# be perfect — ``---`` inside a quoted string would false-split, but in
# Application manifests that shape is essentially never present.
_DOC_SPLIT_RE = re.compile(r"(?m)^---\s*$")

# Argoproj apiVersion. Accept any group/version under argoproj.io so new
# versions (v1alpha1 today; v1 eventually) don't need a code change.
_ARGO_APIVERSION_RE = re.compile(
    r"(?m)^\s*apiVersion:\s*argoproj\.io/\S+\s*$"
)

# Both Application and ApplicationSet use the same spec.source.path
# shape (ApplicationSet wraps it in ``template:`` — covered because we
# scan the whole doc regardless of nesting depth).
_ARGO_KIND_RE = re.compile(
    r"(?m)^\s*kind:\s*(Application|ApplicationSet)\s*$"
)

# metadata.name:. We match the first ``name:`` whose indent is >= the
# ``metadata:`` block's indent + 2. Simple heuristic: take the first
# ``name:`` after a ``metadata:`` line. Good enough — no known case
# where a metadata block has a deeper non-name key appearing first.
_METADATA_HEADER_RE = re.compile(r"(?m)^\s*metadata:\s*$")
_NAME_FIELD_RE = re.compile(
    r"""(?m)^\s*name:\s*
        (?:
            "([^"]*)" |          # double-quoted
            '([^']*)' |          # single-quoted
            (\S.*?)              # bare value
        )
        \s*$""",
    re.VERBOSE,
)

# Any indented ``path:`` value. We run this over the whole Application
# document and accept matches because:
#   * Application specs have ``path:`` only under ``spec.source`` /
#     ``spec.sources[*]``. No other canonical field uses that key name.
#   * ApplicationSet templates mirror the same shape.
# Accept quoted and bare values. Leading ``./`` normalized at resolution
# time, not here (preserve the raw as written for display).
_PATH_FIELD_RE = re.compile(
    r"""(?m)^\s*path:\s*
        (?:
            "([^"]*)" |
            '([^']*)' |
            (\S.*?)
        )
        \s*$""",
    re.VERBOSE,
)


class ArgoCDResolver(BaseResolver):
    def extract(
        self,
        source_bytes: bytes,
        file_path: Path | None = None,
    ) -> list[Edge]:
        text = source_bytes.decode("utf-8", errors="replace")
        edges: list[Edge] = []

        # Iterate over documents. We track each doc's byte offset within
        # ``text`` so line numbers on emitted edges reflect the ORIGINAL
        # file, not the slice.
        for doc_start, doc_end in _iter_doc_spans(text):
            doc = text[doc_start:doc_end]
            if not _is_argocd_app(doc):
                continue

            app_name = _extract_app_name(doc)
            for m in _PATH_FIELD_RE.finditer(doc):
                raw = m.group(1) or m.group(2) or m.group(3) or ""
                raw = raw.strip()
                if not raw:
                    continue
                # Skip the Application's own metadata.name:
                # _PATH_FIELD_RE matches ``path:``, not ``name:`` — this
                # is a defensive check in case a map happens to have a
                # key literally named ``path`` outside source context.
                # In practice Application specs only use ``path`` under
                # source/sources, so every match here is a real ref.
                offset = doc_start + m.start()
                line = text.count("\n", 0, offset) + 1
                edges.append(
                    Edge(
                        kind="argocd_app_source",
                        raw=raw,
                        symbol=app_name,
                        line=line,
                    )
                )

        return edges


def _iter_doc_spans(text: str):
    """Yield ``(start, end)`` byte offsets for each YAML document.

    A single-document file yields one span covering the full text.
    Multi-document files split on ``^---\\s*$`` lines; the separator
    line itself is not part of any document span.
    """
    splits = list(_DOC_SPLIT_RE.finditer(text))
    if not splits:
        yield (0, len(text))
        return
    cursor = 0
    for m in splits:
        if m.start() > cursor:
            yield (cursor, m.start())
        cursor = m.end()
    if cursor < len(text):
        yield (cursor, len(text))


def _is_argocd_app(doc: str) -> bool:
    """True if the document carries both an argoproj.io apiVersion and a
    kind of Application or ApplicationSet.
    """
    return bool(
        _ARGO_APIVERSION_RE.search(doc) and _ARGO_KIND_RE.search(doc)
    )


def _extract_app_name(doc: str) -> str | None:
    """Return the Application's ``metadata.name``, or None if absent or
    templated in a way that leaves no literal value to capture.
    """
    meta = _METADATA_HEADER_RE.search(doc)
    if meta is None:
        return None
    m = _NAME_FIELD_RE.search(doc, meta.end())
    if m is None:
        return None
    value = (m.group(1) or m.group(2) or m.group(3) or "").strip()
    return value or None

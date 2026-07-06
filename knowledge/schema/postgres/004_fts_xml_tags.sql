-- 004_fts_xml_tags.sql — make XML tag names (and generic type params) FTS-visible.
--
-- PG's default text-search parser classifies <Word> as an XML *tag* token,
-- and the 'english' configuration maps tag tokens to no dictionary — they
-- are silently dropped from the tsvector. Consequences on shared PG (SQLite
-- FTS5 tokenizes by punctuation and was never affected):
--
--   * msbuild_project chunks: element names like <TargetFramework> or
--     <ProjectReference> were unsearchable via grep / the FTS arm of ask.
--   * Any generic type mention such as List<string> lost the <string> part.
--
-- Fix: neutralize angle brackets with translate(text, '<>', '  ') before
-- to_tsvector, so tag/generic names tokenize as ordinary words — matching
-- SQLite behavior. Query side needs no change: fts._to_tsquery already
-- strips non-word characters from patterns.
--
-- Idempotent: the DO block inspects the live generated expression and only
-- rebuilds the column when it predates this migration (no 'translate' in
-- pg_get_expr output). Rebuilding recomputes search_vector for every
-- existing chunks row from stored columns — no re-embed, no re-index of
-- content; expect roughly a minute per few hundred thousand chunks.
-- DROP COLUMN also drops the dependent GIN index, so it is recreated here.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_attribute a
        JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE a.attrelid = 'chunks'::regclass
          AND a.attname  = 'search_vector'
          AND pg_get_expr(d.adbin, d.adrelid) NOT LIKE '%translate%'
    ) THEN
        ALTER TABLE chunks DROP COLUMN search_vector;
        ALTER TABLE chunks ADD COLUMN search_vector tsvector
            GENERATED ALWAYS AS (
                to_tsvector('english', translate(
                    coalesce(name, '')           || ' ' ||
                    coalesce(qualified_name, '') || ' ' ||
                    stored_text,
                    '<>', '  '))
            ) STORED;
        CREATE INDEX idx_chunks_search_gin
            ON chunks USING GIN (search_vector);
    END IF;
END $$;

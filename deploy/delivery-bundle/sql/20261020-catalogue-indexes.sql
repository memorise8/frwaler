-- Catalogue browsing indexes (GET /documents keyset pagination).
--
-- NOT part of `python -m delivery.be.migrate`: on a 4M..100M row table every
-- statement below scans the table once. Run deliberately, off-peak, with
-- autocommit (CONCURRENTLY cannot run inside a transaction block):
--
--   psql "$LIBERTREE_PG_DSN" -v ON_ERROR_STOP=1 -f 20261020-catalogue-indexes.sql
--
-- Each build takes a SHARE UPDATE EXCLUSIVE lock only (reads and writes keep
-- working). If a build is interrupted it leaves an INVALID index that
-- IF NOT EXISTS would then skip. With ON_ERROR_STOP the final check below does
-- not run after a failure, so check by hand before rerunning:
--   SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
--   WHERE i.indrelid = 'documents'::regclass AND NOT i.indisvalid;
-- then DROP INDEX CONCURRENTLY each one and rerun. Re-running is otherwise a no-op.
--
-- Priority (create at least the REQUIRED ones; the rest can follow later):
--   REQUIRED    1, 2, 3, 5, 9        default/published/seq browsing per site and landing
--   RECOMMENDED 4, 6, 10, 12         published sort without site; has_pdf/has_text per site
--   OPTIONAL    7, 8, 11, 13         empty-tail probes; has_pdf/has_text without site
-- The pre-existing idx_doc_collected (collected_at) becomes redundant with
-- index 2 for this API; it is not dropped here (other code may rely on it).
--
-- Every list query reads one ordered index range of page_size+1 rows:
--   sort key DESC NULLS LAST, seq_id DESC   (non-NULL segment, then NULL segment)
-- Query shapes are in delivery/be/catalogue.py (segment_sql / relevance_sql).

-- Builds on 4M..100M rows run for minutes: no statement timeout. A bounded
-- lock wait keeps a build from queueing for long behind a stuck transaction.
SET statement_timeout = 0;
SET lock_timeout = '60s';
SET maintenance_work_mem = '1GB';

-- Same definition as delivery/db/schema.py (migration creates it too); kept
-- here so this file is self-contained. ISO YYYY-MM-DD parses identically under
-- every DateStyle, so IMMUTABLE is truthful. Calendar-invalid or partial
-- values ('2025-99-99', '194912', 'not-a-date') map to NULL, exactly like the
-- request-time expression it replaces, so the sort order is unchanged.
CREATE OR REPLACE FUNCTION delivery_published_day(value TEXT)
  RETURNS DATE LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
  SELECT CASE WHEN value ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' AND pg_input_is_valid(value, 'date')
              THEN value::date END
$$;

-- 1. [REQUIRED] sort=collected_desc with site_id (the default browse of one site):
--    WHERE d.site_id = $site AND d.collected_at IS NOT NULL
--      AND (d.collected_at, d.seq_id) < ($k, $seq)
--    ORDER BY collected_at DESC, seq_id DESC LIMIT 21
--    Also serves collected_from/collected_to as an index range, the per-site
--    LATERAL legs of country/doc_type scopes, and the capped count of those.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_doc_site_collected_seq
  ON documents (site_id, collected_at DESC, seq_id DESC);

-- 2. [REQUIRED] sort=collected_desc without site (the landing page /documents):
--    WHERE d.collected_at IS NOT NULL AND (d.collected_at, d.seq_id) < ($k, $seq)
--    ORDER BY collected_at DESC, seq_id DESC LIMIT 21
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_doc_collected_seq
  ON documents (collected_at DESC, seq_id DESC);

-- 3. [REQUIRED] sort=published_desc with site_id, and published_from/to range on a site:
--    WHERE d.site_id = $site AND delivery_published_day(d.published_date) IS NOT NULL
--      AND (delivery_published_day(d.published_date), d.seq_id) < ($k, $seq)
--    ORDER BY k DESC, seq_id DESC LIMIT 21
--    published_date is free TEXT (ISO 'YYYY-MM-DD' when normalized, NULL when
--    partial such as ScienceON '194912', legacy junk possible). A plain
--    (site_id, published_date DESC, ...) index would order the raw text and
--    put junk first, so the index is on the validated day instead.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_doc_site_pubday_seq
  ON documents (site_id, delivery_published_day(published_date) DESC, seq_id DESC);

-- 4. [RECOMMENDED] sort=published_desc without site, and published_from/to without site.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_doc_pubday_seq
  ON documents (delivery_published_day(published_date) DESC, seq_id DESC);

-- 5. [REQUIRED] / 6. [RECOMMENDED] The NULLS LAST tail of published_desc: rows without a usable day.
--    WHERE d.site_id = $site AND delivery_published_day(d.published_date) IS NULL
--      AND d.seq_id < $seq ORDER BY seq_id DESC LIMIT 21
--    IS NULL is not an equality, so index 3 cannot return this segment in
--    seq_id order; without these the tail (most of ScienceON) would be sorted
--    in full on every page.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_doc_site_pubnull_seq
  ON documents (site_id, seq_id DESC) WHERE delivery_published_day(published_date) IS NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_doc_pubnull_seq
  ON documents (seq_id DESC) WHERE delivery_published_day(published_date) IS NULL;

-- 7./8. [OPTIONAL] Same NULLS LAST tail for collected_at (expected to be empty: the
--    column defaults to now()); tiny, but turns the end-of-list probe from a
--    scan of the whole site into an empty index lookup.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_doc_site_collnull_seq
  ON documents (site_id, seq_id DESC) WHERE collected_at IS NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_doc_collnull_seq
  ON documents (seq_id DESC) WHERE collected_at IS NULL;

-- 9. [REQUIRED] sort=seq_desc with site_id, and the relevance candidate read
--    WHERE d.site_id = $site AND <fts/ILIKE match> ORDER BY seq_id DESC LIMIT 1000
--    (the "walk" read for common terms; rare terms use the fts/trigram GIN)
--    (sort=seq_desc without site uses the primary key).
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_doc_site_seq
  ON documents (site_id, seq_id DESC);

-- 10./12. [RECOMMENDED] per site, 11./13. [OPTIONAL] global: has_pdf=true / has_text=true with the default sort. When files are
--    rare in a site (ScienceON API metadata) a filter on index 1/2 would read
--    rows/selectivity entries per page; these partial indexes contain only
--    matching rows. Predicates match the query text exactly
--    (d.pdf_downloaded <> 0 / d.text_extracted <> 0).
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_doc_site_collected_pdf
  ON documents (site_id, collected_at DESC, seq_id DESC) WHERE pdf_downloaded <> 0;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_doc_collected_pdf
  ON documents (collected_at DESC, seq_id DESC) WHERE pdf_downloaded <> 0;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_doc_site_collected_text
  ON documents (site_id, collected_at DESC, seq_id DESC) WHERE text_extracted <> 0;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_doc_collected_text
  ON documents (collected_at DESC, seq_id DESC) WHERE text_extracted <> 0;

-- Expression indexes get their own statistics only after ANALYZE.
ANALYZE documents;

-- Anything listed here was interrupted: DROP INDEX CONCURRENTLY it and rerun.
SELECT c.relname AS invalid_index
FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
WHERE i.indrelid = 'documents'::regclass AND NOT i.indisvalid;

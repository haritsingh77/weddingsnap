-- =============================================================================
-- Fix: match_faces() returned at most 40 rows regardless of k
-- Run in Supabase → SQL Editor → New Query
-- =============================================================================
--
-- faces_embedding_idx is an HNSW index, and pgvector's hnsw.ef_search defaults
-- to 40. ef_search bounds the candidate list the index walk keeps, so it caps
-- how many rows can come back — the LIMIT never gets the chance to matter.
--
-- Measured before this fix: a guest selfie returned exactly 40 rows for k=1000
-- AND for k=5000, with a maximum distance of 0.411. Every guest was capped at
-- 40 photos, and the cut was made by the index rather than by the tolerance.
-- The person used to test has 1,654 photos in the gallery.
--
-- ef_search must therefore be at least the k the caller asks for. The backend
-- calls with k=1000, so 1000 is the floor; a little headroom costs only search
-- time, and only for this function.
--
-- Recall is what matters here: missing someone's photos is far worse for a
-- guest than a slightly slower query, and the tolerance filter downstream
-- discards anything too distant anyway.

create or replace function public.match_faces(q vector(512), k int default 1000)
returns table(filename text, drive_id text, distance double precision)
language sql
stable
set hnsw.ef_search = 2000
as $$
    select f.filename, f.drive_id, (f.embedding <=> q)::double precision as distance
    from public.faces f
    order by f.embedding <=> q
    limit k;
$$;

-- Verify: this should now return 1000 rows, not 40.
--   select count(*) from public.match_faces(
--       (select embedding from public.faces limit 1), 1000);

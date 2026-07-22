-- =============================================================================
-- Fix: match_faces() returned at most 40 rows regardless of k
-- Run in Supabase → SQL Editor → New Query
-- =============================================================================
--
-- The problem
--   faces_embedding_idx is an HNSW index, and pgvector's hnsw.ef_search
--   defaults to 40. ef_search bounds the candidate list the index walk keeps,
--   so it caps the result before LIMIT is ever considered. Measured: a guest
--   selfie returned exactly 40 rows for k=1000 AND k=5000, max distance 0.411.
--   Every guest was capped at 40 photos, and the cut was made by the index
--   rather than by the tolerance. The person tested has 1,654 photos.
--
-- Why not just raise ef_search
--   Supabase's managed role cannot set it:
--     ERROR: 42501: permission denied to set parameter "hnsw.ef_search"
--   That applies to the function-level SET clause and to SET LOCAL alike.
--
-- The fix
--   Force an exact scan instead of an approximate index walk. Adding 0.0 to the
--   ordering expression means it no longer matches the indexed operator, so the
--   planner cannot use the HNSW index and falls back to computing every
--   distance. Ordering is unchanged — adding zero preserves it.
--
--   This is affordable here, and better: the faces table holds ~27k rows, and a
--   512-d cosine distance over that is milliseconds. Exact search also removes
--   the approximation entirely, so recall no longer depends on index tuning —
--   which matters more than speed when the cost of a miss is a guest never
--   seeing their own photos.
--
--   If the table ever grows enough for this to hurt, the alternative is to ask
--   Supabase support to allow ef_search, then restore the index-backed ORDER BY.

create or replace function public.match_faces(q vector(512), k int default 1000)
returns table(filename text, drive_id text, distance double precision)
language sql
stable
as $$
    select f.filename, f.drive_id, (f.embedding <=> q)::double precision as distance
    from public.faces f
    order by (f.embedding <=> q) + 0.0      -- + 0.0 defeats the HNSW index on purpose
    limit k;
$$;

-- Verify: should return 1000, not 40.
--   select count(*) from public.match_faces(
--       (select embedding from public.faces limit 1), 1000);

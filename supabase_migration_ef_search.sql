-- =============================================================================
-- match_faces(): exact search, and return the matched face's cluster
-- Run in Supabase → SQL Editor → New Query
-- =============================================================================
--
-- 1. Exact search instead of the HNSW index walk
--
--    faces_embedding_idx is HNSW and pgvector's hnsw.ef_search defaults to 40.
--    ef_search bounds the candidate list, so it caps the result before LIMIT is
--    considered: a guest selfie returned exactly 40 rows for k=1000 AND k=5000,
--    max distance 0.411. Every guest was capped at 40 photos, and the cut came
--    from the index rather than the tolerance. The person tested has 1,654
--    photos in the gallery.
--
--    ef_search cannot be raised on Supabase's managed role:
--      ERROR: 42501: permission denied to set parameter "hnsw.ef_search"
--
--    Adding 0.0 to the ordering expression stops it matching the indexed
--    operator, so the planner computes every distance instead. Ordering is
--    unchanged. Exact scan over ~27k rows is milliseconds, and removes the
--    approximation entirely — recall no longer depends on index tuning, which
--    matters when a miss means a guest never sees their own photo.
--
-- 2. Returning cluster_id
--
--    The backend expands a match to the rest of a person's cluster, because a
--    single selfie only reaches faces within tolerance of that one shot while a
--    cluster legitimately spans many angles. Doing that needs the cluster of the
--    FACE that matched. Without this column the backend had to look clusters up
--    by drive_id, which returns every face in the photo — so everyone standing
--    next to the guest got their whole cluster pulled in too, turning 395
--    photos into 7,972.

-- Adding cluster_id changes the OUT parameters, and Postgres refuses to alter a
-- function's return type in place:
--   ERROR: 42P13: cannot change return type of existing function
-- So drop first. Nothing else references this function, and the backend simply
-- gets an error for the moment it is absent — so apply it while no one is
-- registering, which for this project is any time before the site is live.
drop function if exists public.match_faces(vector, integer);

create function public.match_faces(q vector(512), k int default 1000)
returns table(
    filename   text,
    drive_id   text,
    cluster_id bigint,
    distance   double precision
)
language sql
stable
as $$
    select f.filename,
           f.drive_id,
           f.cluster_id,
           (f.embedding <=> q)::double precision as distance
    from public.faces f
    order by (f.embedding <=> q) + 0.0      -- + 0.0 defeats the HNSW index on purpose
    limit k;
$$;

-- Verify: should return 1000 rows, with cluster_id populated.
--   select count(*), count(cluster_id)
--   from public.match_faces((select embedding from public.faces limit 1), 1000);

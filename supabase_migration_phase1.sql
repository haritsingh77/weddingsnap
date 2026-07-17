-- ═══════════════════════════════════════════════════════════════════════════
-- Phase 1: face state → Postgres (pgvector)
-- Run in the Supabase SQL editor (same place supabase_schema.sql was run).
--
-- What this enables:
--   * faces table with ANN-indexed 512-d ArcFace embeddings (buffalo_l)
--   * guest-photo disassociations as typed rows (replaces
--     disassociated_photos.json read-modify-write races)
--   * stable cluster identity groundwork (clusters / cluster_merges) for the
--     upcoming cluster-UI port
--   * match_faces() RPC — in-database ANN matching, replaces the Python
--     linear scan over face_encodings.pkl
-- ═══════════════════════════════════════════════════════════════════════════

create extension if not exists vector;


-- 1. Photos gain a filename column (basename of the original file).
--    Existing rows keep drive_path (= Drive file id); the sync script fills
--    filename for rows it creates/updates.
alter table public.photos add column if not exists filename text;
create index if not exists photos_filename_idx on public.photos (filename);


-- 2. Clusters — stable, DB-assigned identity for "recognized people".
--    Fixes the core fragility: today cluster ids are throwaway ints from an
--    in-process FAISS run, and names/merges keyed by them silently break on
--    every re-preprocess.
create table if not exists public.clusters (
    id bigint generated always as identity primary key,
    name text,
    guest_id uuid references public.guests(id) on delete set null,
    rep_face_id bigint,                    -- FK added after faces exists
    created_at timestamptz default timezone('utc'::text, now()) not null
);


-- 3. Faces — one row per detected face (replaces the in-memory .pkl scan).
--    512-d = InsightFace ArcFace (the default backend). dlib 128-d encodings
--    are not supported here; keep them on the legacy pkl path.
create table if not exists public.faces (
    id bigint generated always as identity primary key,
    filename text not null,                -- basename of source photo/video
    drive_id text,                         -- Drive file id when resolvable
    embedding vector(512) not null,
    bbox int[],                            -- top, right, bottom, left (dlib order)
    frame_idx int,                         -- null for still images
    cluster_id bigint references public.clusters(id) on delete set null,
    created_at timestamptz default timezone('utc'::text, now()) not null
);

create index if not exists faces_embedding_idx
    on public.faces using hnsw (embedding vector_cosine_ops);
create index if not exists faces_filename_idx on public.faces (filename);
create index if not exists faces_cluster_idx on public.faces (cluster_id);

alter table public.clusters
    drop constraint if exists clusters_rep_face_fk;
alter table public.clusters
    add constraint clusters_rep_face_fk
    foreign key (rep_face_id) references public.faces(id) on delete set null;


-- 4. Cluster merges as rows (replaces cluster_merges.json).
create table if not exists public.cluster_merges (
    source_cluster_id bigint primary key references public.clusters(id) on delete cascade,
    target_cluster_id bigint not null references public.clusters(id) on delete cascade,
    created_at timestamptz default timezone('utc'::text, now()) not null
);


-- 5. Guest "Not Me" disassociations as typed rows.
--    Replaces disassociated_photos.json, whose whole-file read-modify-write
--    lost concurrent updates and mixed string/int photo ids.
create table if not exists public.guest_photo_disassociations (
    guest_id uuid not null references public.guests(id) on delete cascade,
    photo_id bigint not null references public.photos(id) on delete cascade,
    created_at timestamptz default timezone('utc'::text, now()) not null,
    primary key (guest_id, photo_id)
);


-- 6. In-database ANN matching. Returns the k nearest faces to the query
--    embedding; the caller applies the tolerance filter (keeps the HNSW scan
--    plan simple across pgvector versions). <=> is cosine distance, matching
--    scripts/face_engine/matching.py embedding_distance for insightface.
create or replace function public.match_faces(q vector(512), k int default 1000)
returns table(filename text, drive_id text, distance double precision)
language sql stable as $$
    select f.filename, f.drive_id, (f.embedding <=> q)::double precision as distance
    from public.faces f
    order by f.embedding <=> q
    limit k;
$$;


-- 7. RLS — mirrors the existing permissive policies in supabase_schema.sql so
--    the backend keeps working with the current key. Tighten in Phase 5
--    (service-role-only writes) together with the rest of the schema.
alter table public.clusters enable row level security;
drop policy if exists "Allow public all" on public.clusters;
create policy "Allow public all" on public.clusters for all using (true);

alter table public.faces enable row level security;
drop policy if exists "Allow public all" on public.faces;
create policy "Allow public all" on public.faces for all using (true);

alter table public.cluster_merges enable row level security;
drop policy if exists "Allow public all" on public.cluster_merges;
create policy "Allow public all" on public.cluster_merges for all using (true);

alter table public.guest_photo_disassociations enable row level security;
drop policy if exists "Allow public all" on public.guest_photo_disassociations;
create policy "Allow public all" on public.guest_photo_disassociations for all using (true);

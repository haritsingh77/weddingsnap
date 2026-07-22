-- =============================================================================
-- Per-guest access links + household albums
-- Run in Supabase → SQL Editor → New Query
-- =============================================================================
--
-- Why
--   Everyone shares one invite code, so a guest was identified by the name they
--   typed. That is ambiguous (two people called Ravi Singh collided onto one
--   record) and it is not a credential — the code gates the login screen while
--   every API endpoint behind it is reachable without one.
--
--   A per-guest token fixes both: it names the guest unambiguously AND acts as
--   the credential the API checks. Opening the link is the whole login.

-- 1. The token. Random and unique; the link is /g/<token>.
alter table public.guests add column if not exists access_token text;
create unique index if not exists guests_access_token_idx
    on public.guests (access_token)
    where access_token is not null;

-- Households share one link — a father opening it sees his wife's and
-- children's photos too, so the album is per-link rather than per-person.
alter table public.guests add column if not exists is_household boolean not null default false;

-- Revoking a link should not require deleting the guest.
alter table public.guests add column if not exists access_revoked boolean not null default false;

-- 2. Which people a link can see.
--    A row per person in the household. The album is the union of their
--    clusters, and filtering by cluster_id narrows it to one person — so a
--    photo containing three family members is stored once and attributed to all
--    three. The earlier family_members/member_photos design duplicated photo
--    mappings per member; this uses the clustering directly instead.
create table if not exists public.guest_clusters (
    guest_id   uuid   not null references public.guests(id)   on delete cascade,
    cluster_id bigint not null references public.clusters(id) on delete cascade,
    label      text,
    created_at timestamptz not null default timezone('utc'::text, now()),
    primary key (guest_id, cluster_id)
);

create index if not exists guest_clusters_guest_idx   on public.guest_clusters (guest_id);
create index if not exists guest_clusters_cluster_idx on public.guest_clusters (cluster_id);

-- 3. RLS.
--    Matches the permissive policies already used elsewhere in this schema, so
--    the backend keeps working with its current key. This is NOT a security
--    boundary — access control lives in the API, which is the only thing
--    holding the anon key. Tightening every table to service-role writes is a
--    separate change and should be done all at once.
alter table public.guest_clusters enable row level security;
drop policy if exists "Allow public all" on public.guest_clusters;
create policy "Allow public all" on public.guest_clusters for all using (true);

-- Verify:
--   select column_name from information_schema.columns
--   where table_name = 'guests' and column_name in
--         ('access_token','is_household','access_revoked');
--   select count(*) from public.guest_clusters;

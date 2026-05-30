-- =============================================================================
-- WeddingSnap SQL Migration: Family Profiles & Member-level Photo Mapping
-- Execute this script in your Supabase Project > SQL Editor > New Query
-- =============================================================================

-- 1. Create Family Members Table
CREATE TABLE IF NOT EXISTS public.family_members (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    guest_id uuid REFERENCES public.guests(id) ON DELETE CASCADE,
    name text NOT NULL,
    created_at timestamp with time zone DEFAULT timezone('utc'::text, now()) NOT NULL
);

-- Enable Row Level Security (RLS)
ALTER TABLE public.family_members ENABLE ROW LEVEL SECURITY;

-- Create Policies for RLS
CREATE POLICY "Allow public read" ON public.family_members FOR SELECT USING (true);
CREATE POLICY "Allow public insert" ON public.family_members FOR INSERT WITH CHECK (true);
CREATE POLICY "Allow public update" ON public.family_members FOR UPDATE USING (true);
CREATE POLICY "Allow public delete" ON public.family_members FOR DELETE USING (true);


-- 2. Create Member Photos Mapping Table (for individual filtering)
CREATE TABLE IF NOT EXISTS public.member_photos (
    member_id uuid REFERENCES public.family_members(id) ON DELETE CASCADE,
    photo_id bigint REFERENCES public.photos(id) ON DELETE CASCADE,
    PRIMARY KEY (member_id, photo_id)
);

-- Enable Row Level Security (RLS)
ALTER TABLE public.member_photos ENABLE ROW LEVEL SECURITY;

-- Create Policies for RLS
CREATE POLICY "Allow public read" ON public.member_photos FOR SELECT USING (true);
CREATE POLICY "Allow public upsert" ON public.member_photos FOR ALL USING (true);

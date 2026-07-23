# WeddingSnap — Mac Continuation Prompts

Paste **Prompt 0** at the start of every new Claude session (a fresh session
starts cold with no memory of this work). Then run the task prompts in this
order: **1 → 5 → 3 → 2 → 4**.

Rationale for the order: ship what's already built, confirm downloads actually
work on Cloud Run (the one real unknown), fix placeholder names before sending
links widely, then add households, then the cosmetic admin-gate cleanup.

---

## Prompt 0 — CONTEXT PRIMER (paste first, every session)

```
I'm resuming the WeddingSnap project on my Mac. You have no prior context. Read
this, then confirm you've oriented before doing anything.

WHAT IT IS: face-recognition wedding photo distribution. Guests open a personal
link and see photos they're in. No selfie scan — recognition ran offline on a
GPU box (now retired) and the results are in the DB.

LIVE:
- Repo: github.com/haritsingh77/weddingsnap (main is source of truth)
- Backend: Google Cloud Run, https://weddingsnap-api-660457030810.us-central1.run.app
  (region us-central1; env vars set in the Cloud Run console; light image,
   NO face-recognition libs — it reads clusters from the DB, never re-derives them)
- Frontend: Vercel, https://weddingsnap-haritsinghgrt0110-6451s-projects.vercel.app
  (VITE_API_URL points at the Cloud Run URL, set in Production env)
- Data: Supabase. photos=11,034, faces=27,377, clusters=227 (69 named),
  guests≈70 (each has a unique access_token), guest_clusters, guest_photos.

AUTH MODEL: every /photos, /faces, /download route requires a credential —
X-Guest-Token (a guest's link token) for reads scoped to their own album, or
X-Admin-Password for anything that mutates. auth lives in backend/app/auth_deps.py.

LOCAL SETUP: backend/.env and backend/service-account.json are gitignored — I
copied them from the old machine. Light deps: pip install -r backend/requirements.txt
(no ONNX). The Mac CANNOT re-preprocess (no GPU) and doesn't need to.

RECURRING BUG TO WATCH: Supabase/PostgREST silently caps any select at 1000 rows.
It has caused ~5 bugs. Use backend/app/services/db_paging.py (fetch_all / chunked)
for anything that grows with the gallery.

Redeploy backend: gcloud run deploy weddingsnap-api --source backend/ --region us-central1
Redeploy frontend: push to main (Vercel auto-deploys) or trigger a redeploy in Vercel.

Confirm you've read this and tell me the current deploy state you can verify.
```

---

## Prompt 1 — SHIP WHAT'S BUILT (do first)

```
Several commits are on main but the frontend on Vercel may be stale. The
backend has the "assign photos to people" + corrected "Just Me" changes; the
frontend has the multi-select Share modal and sends ?filter= to the API.

1. Redeploy the backend to Cloud Run.
2. Make sure Vercel has redeployed from the latest main.
3. Then verify against the LIVE site, using a token from guest_links.csv:
   - a guest link opens and shows their photos
   - "Just Me" shows their solo AND group photos (not empty)
   - thumbnails load fast (the HEAD-check removal is deployed)
   - /photos/all returns 401 without credentials
Report what you find before changing anything.
```

---

## Prompt 5 — VERIFY DOWNLOAD FLOW (do second)

```
Test the "Download All" flow end to end on the LIVE deployment for a guest with
a few thousand photos. Cloud Run throttles CPU to near-zero once a response is
sent, and the zip builds in a background task — DEPLOY.md flags this. Confirm
the zip actually completes and streams. If it stalls, the fix is
--no-cpu-throttling on the service (already in my deploy command) or moving zip
building off the request path. Report timing and whether it finishes.
```

---

## Prompt 3 — FIX PLACEHOLDER NAMES (do third)

```
Some of the 69 named clusters are placeholders: "Guest", "Guest1", "Friend",
"Friend Husband", "Sheikh". These became guest names and cluster names. I want
to rename them without changing anyone's access link (tokens must stay the same).

Names live in three places: clusters.name, guests.name, and
cluster_names_by_photos.json. Show me the current placeholder list with photo
counts and a crop of each so I can identify them, then rename in the DB. The
guests.access_token must not change.
```

---

## Prompt 2 — HOUSEHOLDS (do fourth)

```
Build household albums. The schema is already applied (guest_clusters table,
guests.is_household, /auth/link returns members). What's missing:
- a way to make one guest a household linked to several clusters (wife + kids)
- the Gallery showing the union of their photos, with a way to filter to one
  person inside the album.

/auth/link/{token} already returns a `members` array. Design the admin step to
assign multiple clusters to one guest, then the frontend member filter. Show me
the plan before implementing. Test with a real family before committing.
```

---

## Prompt 4 — TIGHTEN ADMIN GATE (do last)

```
In frontend/src/pages/Gallery.jsx around line 241, isAdmin is decided from the
guest's typed name (contains "saurav"/"mahima") or invite code containing
"ADMIN". The backend already enforces admin properly (require_admin), so this
frontend heuristic is now just cosmetic — but it makes the People tab flash for
guests whose name matches. Change isAdmin to be true only when a valid admin
password is present in localStorage. Verify a normal guest never sees the People
tab or Share/Delete controls.
```

---

## Loose ends not yet covered by a prompt

- **Requirements split / CI guard**: `backend/requirements.txt` is already light;
  a small CI check that fails if anything in `backend/` imports `scripts/` would
  stop the heavy deps ever creeping back into the deployed image.
- **`ALLOWED_ORIGINS`**: confirm it's set to the real Vercel domain in Cloud Run
  (it defaulted to `*`).
- **Custom domain**: if you ever add one, regenerate the links with
  `scripts/bootstrap_guests_from_clusters.py --yes --base-url https://<domain>` —
  the tokens don't change, only the prefix.

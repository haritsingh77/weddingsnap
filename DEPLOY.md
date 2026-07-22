# Deploying the backend

The frontend is already on Vercel. This covers the API.

## Before you start

The backend no longer does face recognition, so the image is small and nothing
compiles. Verified: the app imports and registers all 52 routes with
`insightface`, `onnxruntime`, `faiss`, `sklearn`, `cv2`, `face_recognition` and
even `scripts/` absent — which is exactly what ships.

## Environment variables

```
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_KEY=<anon key>

GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT=<the entire contents of service-account.json>
GOOGLE_DRIVE_FOLDER_ID=<id>
GOOGLE_DRIVE_CACHE_FOLDER_ID=<id>
GOOGLE_DRIVE_THUMBNAILS_FOLDER_ID=<id>
GOOGLE_DRIVE_TEMP_DELETE_FOLDER_ID=<id>

ADMIN_PASSWORD=<long random string>
ARCFACE_MATCH_THRESHOLD=0.5
CLUSTER_THRESHOLD=0.4
MIN_CLUSTER_PHOTOS=3

ALLOWED_ORIGINS=https://<your-vercel-domain>
```

`GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT` takes the JSON **contents**, not a path.
`.dockerignore` excludes `service-account.json` and the Dockerfile deletes it
during build, so the file is never in the image — the env var is the only way
credentials get in. Verified working with the file path pointed at nowhere.

`FACE_BACKEND` and the `INSIGHTFACE_*` variables are no longer read.

Set `ALLOWED_ORIGINS` to your real frontend. It currently falls back to `*`,
which is fine while the API had no credentials to steal but is not now.

## Google Cloud Run

```bash
gcloud run deploy weddingsnap-api \
  --source backend/ \
  --region us-central1 \
  --allow-unauthenticated \
  --memory 512Mi \
  --set-env-vars "SUPABASE_URL=...,ADMIN_PASSWORD=..."
```

Use a **US region** — the always-free allowance only applies there.
`--allow-unauthenticated` refers to Google's own IAM layer; the app does its own
authentication on every route.

### Two things that will bite you

**Background ZIP building stalls.** Cloud Run throttles CPU to near zero once a
response is sent, and `/download/{id}/prepare` returns immediately then builds
the archive in a background task. That build will crawl or never finish. Fix
with `--no-cpu-throttling` (bills for idle time) or move ZIP building off the
request path. Worth testing with a guest who has a few thousand photos before
you rely on it.

**Streaming media burns the free allowance.** `/photos/stream/{id}` pipes bytes
through the API and Cloud Run bills CPU for the whole request. Thumbnails
already redirect to Google's CDN, so browsing is cheap; full-size viewing and
downloads are not. 180,000 vCPU-seconds a month is generous but not unlimited.

## Alternative: Oracle Cloud Always Free

A plain VM — 2 OCPU / 12 GB, always on, no request metering, so neither problem
above applies. You manage Docker, nginx and TLS yourself, and popular regions
often report no capacity.

## After deploying

1. Set `VITE_API_URL` in Vercel to the API URL, and redeploy the frontend.
2. Regenerate the guest links against the real domain — tokens do not change:

   ```bash
   python scripts/bootstrap_guests_from_clusters.py --yes \
       --base-url https://<your-vercel-domain>
   ```

3. Check it is actually locked down. Every one of these must refuse:

   ```bash
   curl -i https://<api>/photos/all                  # expect 401
   curl -i https://<api>/photos/stream/<any-id>      # expect 401
   curl -i -X DELETE https://<api>/photos/<any-id>   # expect 401
   ```

   If any returns 200, stop and fix it before sending anyone a link — those
   endpoints serve and delete the entire album.

4. Open one guest link end to end and confirm photos load.

## Things to know

- `guest_links.csv` is a list of passwords. Each link logs its holder straight
  in. It is gitignored; send links individually, not as a shared file.
- Revoke a link by setting `guests.access_revoked = true`. No deletion needed.
- Thumbnails redirect to Google's CDN, so those specific URLs are reachable
  without a token once issued. They are unguessable, but it is a real caveat.
- Supabase RLS is `USING (true)` on every table. The API is the boundary, which
  holds only because the anon key never reaches a browser. Keep it that way.

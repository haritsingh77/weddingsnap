import sys
from pathlib import Path

# Allow importing scripts.face_engine from project root
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routes import auth, faces, photos, download, admin

app = FastAPI(title="WeddingSnap API", version="1.0.0")

import os

ALLOWED_ORIGINS = [o.strip().strip("'\"") for o in os.getenv("ALLOWED_ORIGINS", "*").split(",")]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=r"https://weddingsnap-.*\.vercel\.app|https://weddingsnap-.*-projects\.vercel\.app|http://localhost(:\d+)?|http://127\.0\.0\.1(:\d+)?",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def add_no_cache_headers(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith(("/faces", "/photos", "/download", "/admin")):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# Register routes
app.include_router(auth.router)
app.include_router(faces.router)
app.include_router(photos.router)
app.include_router(download.router)
app.include_router(admin.router)

@app.get("/")
def root():
    return {"status": "WeddingSnap API is running 🎉"}

@app.get("/health")
def health():
    return {"status": "ok"}
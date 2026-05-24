from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routes import auth, faces, photos, download, admin

app = FastAPI(title="WeddingSnap API", version="1.0.0")

import os

ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
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
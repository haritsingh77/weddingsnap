from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routes import auth, faces, photos, download

app = FastAPI(title="WeddingSnap API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routes
app.include_router(auth.router)
app.include_router(faces.router)
app.include_router(photos.router)
app.include_router(download.router)

@app.get("/")
def root():
    return {"status": "WeddingSnap API is running 🎉"}

@app.get("/health")
def health():
    return {"status": "ok"}
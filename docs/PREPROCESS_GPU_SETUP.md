# WeddingSnap GPU Preprocessing Setup (Windows)

Optimized for **ASUS ROG G531GT** — GTX 1650 4GB, i7-9750H, 8GB RAM.

## Architecture

```
Google Drive  →  download to SSD temp  →  InsightFace (RetinaFace + ArcFace)
                      ↓                         ONNX Runtime CUDA
                 thumbnails → Supabase     face_encodings.pkl → Supabase
```

| Component | Role |
|-----------|------|
| **InsightFace** (`buffalo_s`) | Fast GPU model sized for 4GB VRAM |
| **ONNX Runtime GPU** | CUDA inference (not raw dlib CNN) |
| **dlib** (fallback) | Legacy 128-d encodings if InsightFace unavailable |
| **SSD paths** | Temp downloads, model cache, per-file cache |
| **Resume** | `processed_files.txt` + Supabase sync every 25 files |

### Expected performance

| Setup | Typical speed | ~13k images |
|-------|---------------|-------------|
| Intel Mac CPU + dlib CNN | ~200 s/file | ~29 days |
| Windows + InsightFace GPU | **1–5 s/image** | **~1–3 days** (+ Drive download time) |

Run `scripts/benchmark_preprocess.py` on 10–20 local photos before a full Drive run.

---

## 1. SSD path (required)

Point all temp/cache to your **SSD**, not HDD:

```powershell
# PowerShell — set for current session
$env:WEDDINGSNAP_SSD_ROOT = "D:\weddingsnap_cache"

# Permanent (User environment variable)
[System.Environment]::SetEnvironmentVariable("WEDDINGSNAP_SSD_ROOT", "D:\weddingsnap_cache", "User")
```

Created automatically under SSD root:

- `temp_preprocess/` — Drive downloads (deleted after each file)
- `models/` — InsightFace ONNX weights
- `per_file_cache/` — skip unchanged files
- `api_cache/` — API L1 cache (when backend runs)

Add to `backend/.env`:

```env
WEDDINGSNAP_SSD_ROOT=D:\weddingsnap_cache
FACE_BACKEND=insightface
ARCFACE_MATCH_THRESHOLD=0.4
PREPROCESS_BATCH_SIZE=4
MAX_IMAGE_DIMENSION=1600
VIDEO_SAMPLE_INTERVAL_SEC=2.5
```

**8GB RAM tip:** keep `PREPROCESS_BATCH_SIZE=4`. If OOM, use `2`.

---

## 2. Python environment

```powershell
cd C:\Project\weddingsnap
python -m venv venv
.\venv\Scripts\Activate.ps1

pip install --upgrade pip
pip install -r backend\requirements.txt
pip install -r requirements-preprocess.txt
```

### NVIDIA driver

Your machine reports **CUDA 11.6** (driver 512.78). `onnxruntime-gpu==1.17.3` supports CUDA 11.x.

Update GeForce drivers if `verify_gpu` shows no CUDA provider.

---

## 3. Verify GPU (do this before a full run)

```powershell
python scripts\verify_gpu.py
```

Expected:

```
CUDA available      : True
GPU name            : NVIDIA GeForce GTX 1650
ONNX providers      : ['CUDAExecutionProvider', 'CPUExecutionProvider']
InsightFace GPU     : True
✅ GPU acceleration appears AVAILABLE
```

---

## 4. Benchmark (small sample)

```powershell
# 10 photos from a local folder — no Drive, no Supabase
python scripts\benchmark_preprocess.py --input "D:\sample_wedding_photos" --count 10 --backend insightface
```

Target: **under 5 s/image** average on GPU.

---

## 5. Run full Drive preprocessor

```powershell
cd backend
$env:WEDDINGSNAP_SSD_ROOT = "D:\weddingsnap_cache"
$env:FACE_BACKEND = "insightface"

# Resume from Supabase/Mac progress (default)
python ..\scripts\preprocess_drive.py

# Fresh start (clears resume only if you also cleared Supabase cache)
# python ..\scripts\preprocess_drive.py --no-resume
```

### CLI options

| Flag | Description |
|------|-------------|
| `--no-resume` | Ignore `processed_files.txt` |
| `--backend insightface` | Force InsightFace |
| `--backend dlib --model cnn` | Legacy dlib (slow on CPU) |
| `--batch-size 4` | Image batch size |
| `--ssd-root D:\weddingsnap_cache` | Override SSD path |
| `--limit-photos 100` | Test batch |

### Progress monitoring

- Log: `preprocess_drive.log`
- State: `backend/encodings/preprocessor_state.json`
- Telegram: `/status` (if bot running)

---

## 6. Important: embedding backend change

Switching from **dlib (128-d)** to **InsightFace (512-d)** means:

- **Guest matching** uses the new backend automatically (reads `encodings_meta.json`).
- **Already-processed Mac files** (dlib) and **new GPU files** (InsightFace) should not be mixed in one `face_encodings.pkl`.
- Recommended: let GPU run **resume** only if Mac used the same backend, OR clear cache and reprocess all for consistency.

---

## 7. Memory & thermal safety (8GB / laptop)

| Setting | Default | If OOM / overheating |
|---------|---------|---------------------|
| `PREPROCESS_BATCH_SIZE` | 4 | 2 |
| `MAX_IMAGE_DIMENSION` | 1600 | 1200 |
| `GPU_MEM_FRACTION` | 0.85 | 0.7 |
| `PREPROCESS_GC_EVERY` | 10 | 5 |

- Close Chrome/games during runs.
- Use a cooling pad; pause if GPU > 85°C sustained.
- Pipeline calls `gc.collect()` every N files.

---

## 8. Troubleshooting

| Problem | Fix |
|---------|-----|
| `CUDAExecutionProvider` missing | `pip install onnxruntime-gpu` (not `onnxruntime`) |
| InsightFace import error | `pip install insightface onnx` |
| CUDA out of memory | `--batch-size 2`, use `buffalo_s` (default) |
| Still ~200 s/file | GPU not used — run `verify_gpu.py` |
| Drive download slow | Expected; GPU helps detection only |
| Mixed encodings | Clear Supabase cache + reprocess with one backend |

### Clear cache (fresh run)

```powershell
python scripts\clear_supabase_bucket.py
# Then run setup_drive.py if needed
```

---

## 9. Environment variable reference

| Variable | Default | Purpose |
|----------|---------|---------|
| `WEDDINGSNAP_SSD_ROOT` | `%LOCALAPPDATA%\weddingsnap` | SSD base path |
| `FACE_BACKEND` | `auto` | `insightface`, `dlib`, `auto` |
| `INSIGHTFACE_MODEL` | `buffalo_s` | `buffalo_l` = more accurate, more VRAM |
| `PREPROCESS_BATCH_SIZE` | `4` | Images per GPU batch |
| `MAX_IMAGE_DIMENSION` | `1600` | Resize before detection |
| `VIDEO_SAMPLE_INTERVAL_SEC` | `2.5` | Frame sampling |
| `ARCFACE_MATCH_THRESHOLD` | `0.4` | Guest match sensitivity |
| `PREPROCESS_CHECKPOINT_EVERY` | `25` | Supabase upload interval |

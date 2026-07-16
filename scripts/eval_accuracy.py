#!/usr/bin/env python3
"""
Face-matching accuracy evaluation harness.

Turns "accuracy is an issue" into numbers: precision / recall / F1 measured
against a labelled ground-truth set, swept across match thresholds so you can
pick the best operating point instead of guessing.

Independent of the FastAPI app and Supabase — it reads the encodings .pkl
directly and encodes selfies with the same face engine used in preprocessing.

────────────────────────────────────────────────────────────────────────────
1. Build encodings first (scripts/preprocess.py) → face_encodings.pkl
2. Create a labels file, e.g. eval/labels.json:

   {
     "people": [
       {
         "name": "Priya",
         "selfies": ["eval/selfies/priya_1.jpg", "eval/selfies/priya_2.jpg"],
         "true_photos": ["IMG_0012.jpg", "IMG_0430.jpg", "DSC_1187.jpg"]
       },
       ...
     ]
   }

   "selfies"      = paths to that person's reference selfie(s)
   "true_photos"  = basenames of gallery photos that genuinely contain them
                    (the ground truth — label ~15-20 people for a solid signal)

3. Run:
   python scripts/eval_accuracy.py --encodings backend/encodings/face_encodings.pkl \\
       --labels eval/labels.json
────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from scripts.face_engine.config import PreprocessConfig
from scripts.face_engine.matching import (
    detect_backend_from_records,
    embedding_distance,
)
from scripts.face_engine.pipeline import get_pipeline


def encode_selfie(path: Path, pipeline) -> np.ndarray | None:
    """Encode one selfie the same way the live app does (largest face)."""
    try:
        img = Image.open(path).convert("RGB")
        img = ImageOps.exif_transpose(img)
        w, h = img.size
        if max(w, h) > 1000:
            s = 1000 / max(w, h)
            img = img.resize((int(w * s), int(h * s)), Image.LANCZOS)
        dets = pipeline.backend.detect_and_encode(np.array(img))
        if not dets:
            return None
        largest = max(dets, key=lambda d: (d.bbox[2] - d.bbox[0]) * (d.bbox[3] - d.bbox[1]))
        return largest.encoding
    except Exception as e:
        print(f"  ! failed to encode {path}: {e}")
        return None


def centroid(encs: list[np.ndarray]) -> np.ndarray:
    stacked = np.array(encs, dtype=np.float64)
    unit = stacked / (np.linalg.norm(stacked, axis=1, keepdims=True) + 1e-8)
    c = unit.mean(axis=0)
    return c / (np.linalg.norm(c) + 1e-8)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--encodings", required=True, help="path to face_encodings.pkl")
    ap.add_argument("--labels", required=True, help="path to labels.json")
    ap.add_argument("--min", type=float, default=0.20, help="lowest threshold to sweep")
    ap.add_argument("--max", type=float, default=0.50, help="highest threshold to sweep")
    ap.add_argument("--step", type=float, default=0.025)
    ap.add_argument("--aggregate", choices=["centroid", "min"], default="centroid")
    args = ap.parse_args()

    with open(args.encodings, "rb") as f:
        records = pickle.load(f)
    backend = detect_backend_from_records(records)
    print(f"Loaded {len(records):,} photo records · backend={backend}")

    # photo basename → list of face embeddings in that photo (skip common/group tag)
    photo_faces: dict[str, list[np.ndarray]] = {}
    for r in records:
        name = Path(r["path"]).name
        photo_faces.setdefault(name, []).extend(r.get("encodings", []))

    labels = json.loads(Path(args.labels).read_text())
    pipeline = get_pipeline(PreprocessConfig())

    # Precompute, per person, the min distance from their query to each photo.
    people = []
    for person in labels["people"]:
        encs = [e for p in person["selfies"] if (e := encode_selfie(Path(p), pipeline)) is not None]
        if not encs:
            print(f"  ! {person['name']}: no usable selfie, skipping")
            continue
        query = [centroid(encs)] if (args.aggregate == "centroid" and len(encs) > 1) else encs

        photo_min_dist: dict[str, float] = {}
        for pname, faces in photo_faces.items():
            if not faces:
                continue
            d = min(
                embedding_distance(face, q, backend)
                for face in faces
                for q in query
            )
            photo_min_dist[pname] = d

        people.append({
            "name": person["name"],
            "truth": set(person["true_photos"]),
            "dist": photo_min_dist,
        })
        print(f"  · {person['name']}: {len(encs)} selfie(s), {len(person['true_photos'])} truth photos")

    if not people:
        print("No evaluable people. Check your labels file.")
        return

    # Sweep thresholds, aggregate confusion counts across all people.
    print(f"\n{'thresh':>7}  {'precision':>9}  {'recall':>7}  {'F1':>6}   (TP/FP/FN)")
    best = (0.0, -1.0)
    t = args.min
    while t <= args.max + 1e-9:
        TP = FP = FN = 0
        for p in people:
            predicted = {name for name, d in p["dist"].items() if d <= t}
            TP += len(predicted & p["truth"])
            FP += len(predicted - p["truth"])
            FN += len(p["truth"] - predicted)
        prec = TP / (TP + FP) if (TP + FP) else 0.0
        rec = TP / (TP + FN) if (TP + FN) else 0.0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        flag = ""
        if f1 > best[1]:
            best = (t, f1)
            flag = "  <-- best F1"
        print(f"{t:7.3f}  {prec:9.3f}  {rec:7.3f}  {f1:6.3f}   ({TP}/{FP}/{FN}){flag}")
        t += args.step

    print(f"\nBest threshold ≈ {best[0]:.3f} (F1 {best[1]:.3f}). "
          f"Set ARCFACE_MATCH_THRESHOLD={best[0]:.3f} to use it.")


if __name__ == "__main__":
    main()

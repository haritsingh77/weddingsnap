#!/usr/bin/env python3
"""
Label-free A/B comparison of two face-encoding runs.

The trick
---------
Two faces detected in the SAME photo are almost certainly different people —
a person appears once per frame. That yields tens of thousands of GUARANTEED
NEGATIVE pairs without a single hand label, which is enough to measure the
thing that actually matters: how often a model confuses two different people.

Comparing raw distances across models is meaningless — each model has its own
distance scale. So instead we calibrate: pick, per model, the threshold that
produces the SAME false-match rate on guaranteed negatives, then ask which
model retrieves more cross-photo pairs at that equal-error operating point.
More retrieval at equal false-match rate = strictly better embedding space.

Also reports cluster purity violations: a cluster containing two faces from
one photo is an unambiguous error, again needing no labels.

Usage
-----
    python scripts/compare_models.py \
        --a eval/face_encodings.buffalo_s.subset.pkl --a-name buffalo_s \
        --b backend/encodings/face_encodings.pkl     --b-name buffalo_l
"""

from __future__ import annotations

import argparse
import pickle
from pathlib import Path

import numpy as np


def load_faces(path: Path) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Return (unit-normalised embeddings, photo index per face, photo names)."""
    with open(path, "rb") as f:
        records = pickle.load(f)

    embs, photo_of, names = [], [], []
    for rec in records:
        encs = rec.get("encodings", [])
        if not encs:
            continue
        pid = len(names)
        names.append(Path(rec["path"]).name)
        for e in encs:
            embs.append(np.asarray(e, dtype=np.float32))
            photo_of.append(pid)

    X = np.vstack(embs)
    X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-8
    return X, np.asarray(photo_of), names


def pair_distances(X: np.ndarray, photo_of: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Cosine distances split into same-photo (guaranteed negatives) and cross-photo."""
    D = 1.0 - (X @ X.T)
    iu = np.triu_indices(len(X), k=1)
    d = D[iu]
    same = photo_of[iu[0]] == photo_of[iu[1]]
    return d[same], d[~same]


def purity_violations(X: np.ndarray, photo_of: np.ndarray, threshold: float) -> tuple[int, int]:
    """Cluster with the same kNN + union-find the sync script uses, then count
    clusters holding two faces from one photo (definitionally wrong)."""
    from sklearn.neighbors import NearestNeighbors

    n = len(X)
    k = min(50, n)
    nn = NearestNeighbors(n_neighbors=k, metric="cosine").fit(X)
    dists, idxs = nn.kneighbors(X)

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(1, k):
            if dists[i][j] <= threshold:
                ri, rj = find(i), find(int(idxs[i][j]))
                if ri != rj:
                    parent[ri] = rj

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    bad = 0
    for members in groups.values():
        photos = photo_of[members]
        if len(photos) != len(set(photos.tolist())):
            bad += 1
    return len(groups), bad


def report(name: str, path: Path, fmr_targets: list[float]) -> dict:
    X, photo_of, names = load_faces(path)
    neg, cross = pair_distances(X, photo_of)

    print(f"\n=== {name} ===")
    print(f"  photos {len(names):,} · faces {len(X):,}")
    print(f"  guaranteed-negative pairs (same photo): {len(neg):,}")
    print(f"  cross-photo pairs: {len(cross):,}")
    print(f"  negative distance percentiles  "
          f"p1 {np.percentile(neg,1):.3f} · p5 {np.percentile(neg,5):.3f} · "
          f"median {np.median(neg):.3f}")

    out = {"name": name, "faces": len(X), "ops": {}}
    for target in fmr_targets:
        t = float(np.percentile(neg, target * 100))
        retrieved = int((cross <= t).sum())
        per_face = retrieved / len(X)
        out["ops"][target] = (t, retrieved, per_face)
        print(f"  @FMR {target*100:>5.2f}%  threshold {t:.3f}  "
              f"cross-photo matches {retrieved:>8,}  ({per_face:.2f}/face)")

    # False matches at the threshold actually shipped in config
    shipped = 0.4
    fmr_at_shipped = float((neg <= shipped).mean())
    print(f"  @shipped threshold 0.400  false-match rate on known negatives: "
          f"{fmr_at_shipped*100:.3f}%  ({int((neg<=shipped).sum()):,} pairs)")
    out["fmr_at_shipped"] = fmr_at_shipped

    ngroups, bad = purity_violations(X, photo_of, shipped)
    print(f"  clustering @0.400: {ngroups:,} groups · "
          f"{bad:,} contain 2+ faces from one photo (definite errors)")
    out["groups"], out["bad"] = ngroups, bad
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True)
    ap.add_argument("--a-name", default="A")
    ap.add_argument("--b", required=True)
    ap.add_argument("--b-name", default="B")
    args = ap.parse_args()

    targets = [0.001, 0.01, 0.05]
    ra = report(args.a_name, Path(args.a), targets)
    rb = report(args.b_name, Path(args.b), targets)

    print("\n" + "=" * 62)
    print("VERDICT — retrieval at equal false-match rate (higher is better)")
    print("=" * 62)
    for t in targets:
        _, _, pa = ra["ops"][t]
        _, _, pb = rb["ops"][t]
        delta = (pb - pa) / pa * 100 if pa else float("nan")
        winner = rb["name"] if pb > pa else ra["name"]
        print(f"  FMR {t*100:>5.2f}%   {ra['name']} {pa:.2f}/face  vs  "
              f"{rb['name']} {pb:.2f}/face   -> {winner} ({delta:+.1f}%)")

    print(f"\n  false-match rate at shipped 0.400: "
          f"{ra['name']} {ra['fmr_at_shipped']*100:.3f}%  vs  "
          f"{rb['name']} {rb['fmr_at_shipped']*100:.3f}%")
    print(f"  impure clusters:                   "
          f"{ra['name']} {ra['bad']:,}/{ra['groups']:,}  vs  "
          f"{rb['name']} {rb['bad']:,}/{rb['groups']:,}")


if __name__ == "__main__":
    main()

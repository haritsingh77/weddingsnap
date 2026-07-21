#!/usr/bin/env python3
"""
Filter an encodings pkl down to the photo set of another pkl.

Why this exists
---------------
Comparing two face models is only meaningful on the SAME photos. The buffalo_s
pkl covers the whole corpus; a buffalo_l sample run covers a few hundred files.
Running eval_accuracy.py against both as-is would measure coverage, not model
quality — every photo missing from the smaller set counts as a false negative.

This trims the larger pkl to exactly the filenames present in the smaller one,
so both evals see an identical photo universe.

Usage
-----
    python scripts/subset_pkl.py \
        --source backend/encodings/face_encodings.buffalo_s.bak \
        --like   backend/encodings/face_encodings.pkl \
        --out    eval/face_encodings.buffalo_s.subset.pkl

Records are matched on basename, since the two runs can use different path
roots (a local folder vs "GoogleDrive/<name>").
"""

from __future__ import annotations

import argparse
import collections
import pickle
import sys
from pathlib import Path


def load(path: Path) -> list[dict]:
    if not path.exists():
        sys.exit(f"Not found: {path}")
    with open(path, "rb") as f:
        return pickle.load(f)


def describe(records: list[dict], label: str) -> None:
    faces = sum(len(r.get("encodings", [])) for r in records)
    models = collections.Counter(r.get("detection_model") for r in records)
    dims = collections.Counter(
        len(e) for r in records for e in r.get("encodings", [])
    )
    print(f"{label}: {len(records):,} files, {faces:,} faces")
    print(f"  models: {dict(models)}")
    print(f"  dims  : {dict(dims)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="pkl to trim down")
    ap.add_argument("--like", required=True, help="pkl whose photo set defines the subset")
    ap.add_argument("--out", required=True, help="where to write the trimmed pkl")
    args = ap.parse_args()

    source = load(Path(args.source))
    target = load(Path(args.like))

    describe(source, "source")
    describe(target, "like  ")

    wanted = {Path(r["path"]).name for r in target}
    subset = [r for r in source if Path(r["path"]).name in wanted]

    found = {Path(r["path"]).name for r in subset}
    missing = wanted - found
    print()
    print(f"Wanted {len(wanted):,} photos; matched {len(found):,} in source.")
    if missing:
        print(f"! {len(missing):,} photos absent from source — excluded from both sides.")
        for name in sorted(missing)[:10]:
            print(f"    {name}")
        if len(missing) > 10:
            print(f"    ... and {len(missing) - 10:,} more")
        print("  Restrict eval ground truth to the matched set, or recall is understated.")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as f:
        pickle.dump(subset, f)
    print()
    describe(subset, "written")
    print(f"-> {out}")


if __name__ == "__main__":
    main()

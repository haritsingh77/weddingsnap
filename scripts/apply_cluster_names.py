#!/usr/bin/env python3
"""
Turn renamed face crops into cluster names.

How the mapping survives renaming
---------------------------------
extract_cluster_faces.py writes a manifest keyed by filename, but the whole
point of the workflow is that you rename those files — which destroys that key.
So the link is recovered from image CONTENT instead: the crops are regenerated
deterministically from the same encodings, hashed, and matched byte-for-byte
against the files on disk. Renaming doesn't change bytes, so this recovers the
cluster for every file no matter what it's called now.

Why names aren't stored against cluster numbers
-----------------------------------------------
Cluster ids are positional — they're whatever the clustering happened to emit,
and the next run renumbers them. Storing "cluster 25725 = Mahima" would put her
name on a different person after the next preprocess. That is exactly the bug
that left "Mahima Singh" on the wrong face in cluster_names.json.

Instead each name is stored with the PHOTOS its cluster contains. Photos have
stable identity, so after a re-run the name can be re-attached to whichever
cluster holds those same photos.

Usage
-----
    # 1. Check what your renaming would do (writes nothing):
    python scripts/apply_cluster_names.py --crops cluster_faces_full \\
        --encodings backend/encodings/face_encodings.snapshot.pkl

    # 2. Save the mapping (still no DB writes):
    python scripts/apply_cluster_names.py --crops cluster_faces_full \\
        --encodings backend/encodings/face_encodings.snapshot.pkl --save

    # 3. After sync_encodings_to_db.py has populated faces/clusters:
    python scripts/apply_cluster_names.py --apply-db
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

NAMES_FILE = project_root / "cluster_names_by_photos.json"
IGNORE_DIRNAME = "ignore"

# Filenames as written by extract_cluster_faces.py, e.g.
# "0007_c25725_3499photos_MIXED_alt1.jpg". Anything still matching this has not
# been renamed, so it is not a name. Testing membership of the regenerated
# manifest instead fails across batches, because exclusions shift the rank
# prefix while the image bytes stay identical.
GENERATED_RE = re.compile(r"^\d{4}_c\d+_\d+photos(_MIXED)?(_alt\d+)?$")


def file_hash(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def person_key(stem: str) -> str:
    """Internal identity for a crop file — distinguishes people, never shown.

    Strips only a trailing photo counter ('Nita_Singh_2' -> 'Nita_Singh'), so a
    private qualifier that separates two people with the same real name
    ('Ravi_Singh#mama') survives and keeps their clusters apart.
    """
    return re.sub(r"[\s_-]*\d+$", "", stem).strip() or stem


def display_name(key: str) -> str:
    """What a guest actually sees.

    Filenames are a workaround for not having a UI, so none of their mechanics
    should leak into the gallery: underscores become spaces, and anything after
    '#' is a private disambiguator for same-named people and is dropped. So
    'Ravi_Singh#mama.jpg' and 'Ravi_Singh#chacha.jpg' stay separate people who
    both simply display as "Ravi Singh" — the UI never shows "mama".
    """
    name = key.split("#", 1)[0]
    name = re.sub(r"[_]+", " ", name)
    return re.sub(r"\s+", " ", name).strip() or key


def regenerate(encodings: Path, limit: int, candidates: int) -> tuple[Path, dict]:
    """Re-run the extractor into a temp dir to rebuild hash -> cluster.

    Deliberately generates a SUPERSET: no --exclude-named and the maximum
    candidates. Later batches are produced with exclusions and fewer
    candidates, so their crops are a subset of this set and still hash-match.
    Regenerating with a batch's own narrower arguments would miss the others.
    """
    tmp = Path(tempfile.mkdtemp(prefix="wsnap_crops_"))
    cmd = [
        sys.executable, str(project_root / "scripts" / "extract_cluster_faces.py"),
        "--encodings", str(encodings), "--out", str(tmp),
        "--limit", str(limit), "--candidates", str(candidates),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    man = tmp / "manifest.json"
    if not man.exists():
        shutil.rmtree(tmp, ignore_errors=True)
        sys.exit(f"Could not regenerate crops:\n{res.stdout[-800:]}\n{res.stderr[-800:]}")
    return tmp, json.loads(man.read_text(encoding="utf-8"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--crops", nargs="+", default=["cluster_faces_full"],
                    help="one or more crop folders (batches apply together)")
    ap.add_argument("--encodings", default="backend/encodings/face_encodings.snapshot.pkl")
    ap.add_argument("--limit", type=int, default=300,
                    help="regeneration depth; must cover every batch exported")
    ap.add_argument("--candidates", type=int, default=3)
    ap.add_argument("--save", action="store_true", help="write the name->photos mapping")
    ap.add_argument("--apply-db", action="store_true", help="push saved names into clusters table")
    args = ap.parse_args()

    if args.apply_db:
        apply_db()
        return

    crop_dirs = [Path(c) for c in args.crops]
    for c in crop_dirs:
        if not c.is_dir():
            sys.exit(f"No such folder: {c}")

    print("Regenerating reference crops to recover the mapping...")
    tmp, ref_manifest = regenerate(Path(args.encodings), args.limit, args.candidates)
    try:
        hash_to_entry = {}
        for fn, info in ref_manifest.items():
            p = tmp / fn
            if p.exists():
                hash_to_entry[file_hash(p)] = info

        ignored_files = []
        for c in crop_dirs:
            d = c / IGNORE_DIRNAME
            if d.is_dir():
                ignored_files += list(d.glob("*.jpg"))

        named: dict[str, dict] = defaultdict(lambda: {"clusters": set(), "files": []})
        key_aliases: dict[str, str] = {}
        unmatched, untouched = [], []
        ignored_clusters: set[int] = set()

        for f in sorted(f for c in crop_dirs for f in c.glob("*.jpg")):
            entry = hash_to_entry.get(file_hash(f))
            if entry is None:
                unmatched.append(f.name)
                continue
            if GENERATED_RE.match(f.stem):      # still the generated name
                untouched.append(entry["cluster"])
                continue
            n = person_key(f.stem)
            # Group case-insensitively: "Pandit_Ji" and "Pandit_ji" are one
            # person typed twice, and letting them through would create two
            # people in the gallery. Genuinely distinct same-named guests are
            # separated with a '#qualifier', not by capitalisation.
            n = key_aliases.setdefault(n.casefold(), n)
            named[n]["clusters"].add(entry["cluster"])
            named[n]["files"].append(f.name)
            named[n].setdefault("photos", set()).update(entry["member_photos"])
            named[n].setdefault("impure", False)
            named[n]["impure"] |= bool(entry.get("impure"))

        for f in ignored_files:
            entry = hash_to_entry.get(file_hash(f))
            if entry:
                ignored_clusters.add(entry["cluster"])

        # ── report ───────────────────────────────────────────────────────────
        print(f"\n{'='*66}\nNAMES FOUND\n{'='*66}")
        total_photos = set()
        for n in sorted(named, key=lambda k: -len(named[k].get("photos", ()))):
            d = named[n]
            total_photos |= d["photos"]
            shown = display_name(n)
            flag = "  << MIXED CLUSTER" if d["impure"] else ""
            merge = f"  (merges {len(d['clusters'])} clusters)" if len(d["clusters"]) > 1 else ""
            alias = "" if shown == n else f'   [shows as "{shown}"]'
            print(f"  {n:<26} {len(d['photos']):>5,} photos · clusters {sorted(d['clusters'])}{merge}{alias}{flag}")

        print(f"\n  {len(named)} people covering {len(total_photos):,} distinct photos")
        if untouched:
            print(f"  {len(set(untouched))} cluster(s) left unnamed (still generated filenames)")
        if ignored_clusters:
            print(f"  {len(ignored_clusters)} cluster(s) marked ignore/")
        if unmatched:
            print(f"\n  ! {len(unmatched)} file(s) did not match any known crop "
                  f"(edited or added?): {unmatched[:5]}")

        impure = [n for n, d in named.items() if d["impure"]]
        if impure:
            print(f"\n{'='*66}\nWARNING — these names sit on MIXED clusters\n{'='*66}")
            for n in impure:
                print(f"  {n}: this cluster provably holds MORE THAN ONE person "
                      f"(two of its faces appear in the same photo).")
            print("  Naming it labels everyone in it as that one person.")
            print("  Split it with label_clusters.py using a reference photo of each,")
            print("  or move the crop to ignore/ for now.")

        near = find_near_duplicates(list(named))
        if near:
            print(f"\n{'='*66}\nPOSSIBLE TYPOS — very similar names\n{'='*66}")
            for a, b in near:
                print(f"  {a!r} vs {b!r} — same person under two spellings?")

        if args.save:
            payload = {
                n: {
                    "display_name": display_name(n),
                    "clusters": sorted(d["clusters"]),
                    "photos": sorted(d["photos"]),
                    "impure": d["impure"],
                }
                for n, d in named.items()
            }
            payload["__ignored_clusters__"] = sorted(ignored_clusters)
            NAMES_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            print(f"\nSaved -> {NAMES_FILE.name}")
            print("After sync_encodings_to_db.py, run with --apply-db to push these names.")
        else:
            print("\nPreview only — nothing saved. Re-run with --save to keep this mapping.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def find_near_duplicates(names: list[str]) -> list[tuple[str, str]]:
    """Flag names one or two edits apart — 'Mahima_Sing' vs 'Mahima_Singh'."""
    def norm(s: str) -> str:
        return re.sub(r"[\s_-]+", "", s).lower()

    out = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            na, nb = norm(a), norm(b)
            if na == nb:
                out.append((a, b))
            elif abs(len(na) - len(nb)) <= 2 and (na.startswith(nb) or nb.startswith(na)):
                out.append((a, b))
    return out


def apply_db() -> None:
    """Attach saved names to DB clusters by photo overlap (ids are not stable)."""
    import os
    from dotenv import load_dotenv

    if not NAMES_FILE.exists():
        sys.exit(f"{NAMES_FILE.name} not found — run with --save first.")
    saved = json.loads(NAMES_FILE.read_text(encoding="utf-8"))
    saved.pop("__ignored_clusters__", None)

    load_dotenv(project_root / "backend" / ".env")
    from supabase import create_client

    sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

    rows, offset = [], 0
    while True:
        res = sb.table("faces").select("filename, cluster_id").range(offset, offset + 999).execute()
        batch = res.data or []
        rows.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000

    db_clusters: dict[int, set[str]] = defaultdict(set)
    for r in rows:
        if r.get("cluster_id") is not None:
            db_clusters[r["cluster_id"]].add(r["filename"])
    if not db_clusters:
        sys.exit("No clustered faces in the DB yet — run sync_encodings_to_db.py first.")

    applied = skipped = 0
    used: set[int] = set()
    for name, info in sorted(saved.items(), key=lambda kv: -len(kv[1]["photos"])):
        want = set(info["photos"])
        best, best_n = None, 0
        for cid, files in db_clusters.items():
            if cid in used:
                continue
            n = len(want & files)
            if n > best_n:
                best, best_n = cid, n
        if best is None or best_n < max(2, len(want) // 4):
            print(f"  ! {name}: no confident DB cluster (best overlap {best_n}) — skipped")
            skipped += 1
            continue
        shown = info.get("display_name") or display_name(name)
        sb.table("clusters").update({"name": shown}).eq("id", best).execute()
        used.add(best)
        applied += 1
        print(f'  {name:<26} -> cluster {best} as "{shown}" '
              f"({best_n}/{len(want)} photos matched)")

    print(f"\nApplied {applied} name(s); {skipped} skipped.")


if __name__ == "__main__":
    main()

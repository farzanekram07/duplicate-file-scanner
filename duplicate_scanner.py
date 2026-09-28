"""
Duplicate File Scanner
----------------------
A staged duplicate-file finder for large folders and network shares.
Most files are never fully read, because candidates are narrowed in stages:

  STAGE 1 - SIZE GROUPING (near-free)
      A file whose size matches no other file cannot be a duplicate.
      Status: "unique_size".

  STAGE 2 - PARTIAL HASH (cheap)
      For files sharing a size, hash the first 4 KB + last 4 KB.
      If unique within its size group -> status "unique_partial".

  STAGE 3 - FULL HASH (only true candidates)
      Files sharing BOTH size and partial hash are fully hashed to
      confirm exact duplicates. Status: "verified".

  SKIP-IF-UNCHANGED
      If a previous inventory exists, files whose size and modified time
      are unchanged are reused without reading them. A cached file is only
      re-checked when a new or changed file has the same size (so new
      duplicates of old files are still found).

  MULTI-THREADING
      Hashing is I/O-bound, so partial and full hashing use several threads.

HOW TO RUN
1. pip install -r requirements.txt
2. Edit FOLDERS_TO_SCAN and OUTPUT_FOLDER below.
3. Run:  python duplicate_scanner.py

OUTPUT (in OUTPUT_FOLDER):
  - inventory_master.csv   one row per file, with hash_status column
  - duplicates_report.csv  only files confirmed as exact duplicates
  - errors.csv             files that could not be read

The scanner only reads files. It never modifies or deletes anything.
"""

import os
import hashlib
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

# ---------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------
# List of (label, folder_path) pairs. Add as many as you need.
FOLDERS_TO_SCAN = [
    ("Folder A", r"C:\Users\YourName\Documents\FolderA"),
    ("Folder B", r"\\SERVER\Share\FolderB"),
]

OUTPUT_FOLDER = r"C:\Users\YourName\Documents\ScanResults"

EXCEL_EXT = {".xlsx", ".xls", ".xlsm"}
WORD_EXT = {".docx", ".doc"}
PDF_EXT = {".pdf"}
ALL_TRACKED_EXT = EXCEL_EXT | WORD_EXT | PDF_EXT

PARTIAL_CHUNK = 4096            # bytes read from start and end for partial hash
FULL_HASH_CHUNK = 1024 * 1024   # 1 MB reads for full hashing
MAX_WORKERS = 8                 # threads for hashing (I/O bound)
PROGRESS_EVERY = 500

MASTER_CSV = "inventory_master.csv"
ERRORS_CSV = "errors.csv"
DUPLICATES_CSV = "duplicates_report.csv"

INVENTORY_FIELDS = [
    "source_label", "file_name", "folder", "full_path", "extension",
    "size_kb", "size_bytes", "last_modified", "hash_status", "md5_hash",
]


# ---------------------------------------------------------------------
# HASHING HELPERS
# ---------------------------------------------------------------------
def partial_hash(path: Path, size: int) -> str:
    """Hash the first and last PARTIAL_CHUNK bytes of a file."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        h.update(f.read(PARTIAL_CHUNK))
        if size > PARTIAL_CHUNK:
            f.seek(max(size - PARTIAL_CHUNK, 0))
            h.update(f.read(PARTIAL_CHUNK))
    return h.hexdigest()


def full_md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(FULL_HASH_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def run_threaded(recs: list, func, label: str):
    """Run func(rec) across threads. Returns ({path: result}, [(rec, error)])."""
    results, failed = {}, []
    if not recs:
        return results, failed
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(func, r): r for r in recs}
        for i, fut in enumerate(as_completed(futures), 1):
            rec = futures[fut]
            try:
                results[rec["full_path"]] = fut.result()
            except Exception as e:
                failed.append((rec, str(e)))
            if i % PROGRESS_EVERY == 0:
                print(f"  ...{label} {i}/{len(recs)}")
    return results, failed


# ---------------------------------------------------------------------
# STAGE 0: WALK FOLDERS -> METADATA ONLY
# ---------------------------------------------------------------------
def collect_metadata(errors: list) -> list:
    records = []
    for label, root_folder in FOLDERS_TO_SCAN:
        root_path = Path(root_folder)
        if not root_path.exists():
            print(f"  [SKIP] Folder not found: {root_folder}")
            continue
        print(f"Walking [{label}]: {root_folder}")
        count = 0
        for dirpath, _dirs, filenames in os.walk(root_path):
            for fname in filenames:
                fpath = Path(dirpath) / fname
                ext = fpath.suffix.lower()
                if ext not in ALL_TRACKED_EXT:
                    continue
                try:
                    stat = fpath.stat()
                except Exception as e:
                    errors.append({"source_label": label, "file_name": fname,
                                   "full_path": str(fpath), "error": str(e)})
                    continue
                records.append({
                    "source_label": label,
                    "file_name": fname,
                    "folder": str(fpath.parent),
                    "full_path": str(fpath),
                    "extension": ext,
                    "size_bytes": stat.st_size,
                    "size_kb": round(stat.st_size / 1024, 1),
                    "last_modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                })
                count += 1
                if count % PROGRESS_EVERY == 0:
                    print(f"  ...{count} files found so far in this folder")
        print(f"  [{label}] total tracked files: {count}")
    return records


# ---------------------------------------------------------------------
# CACHE FROM PREVIOUS RUN
# ---------------------------------------------------------------------
def load_cache(master_path: str) -> dict:
    """Returns {full_path: (size_bytes, last_modified, hash_status, md5_hash)}."""
    if not os.path.exists(master_path):
        return {}
    df = pd.read_csv(master_path, keep_default_na=False)
    cache = {}
    for row in df.itertuples(index=False):
        cache[row.full_path] = (int(row.size_bytes), str(row.last_modified),
                                row.hash_status, row.md5_hash)
    print(f"Loaded cache from previous run: {len(cache)} files.")
    return cache


# ---------------------------------------------------------------------
# STAGES 1-3
# ---------------------------------------------------------------------
def hash_records(records: list, cache: dict, errors: list) -> list:
    results, to_process = [], []

    def fail(rec, msg):
        errors.append({"source_label": rec["source_label"], "file_name": rec["file_name"],
                       "full_path": rec["full_path"], "error": msg})
        rec["hash_status"] = "error"
        rec["md5_hash"] = ""
        results.append(rec)

    # Skip-if-unchanged (previous errors are always retried)
    cached_hits = []
    for rec in records:
        c = cache.get(rec["full_path"])
        if c and c[2] != "error" and c[0] == rec["size_bytes"] and c[1] == rec["last_modified"]:
            rec["hash_status"], rec["md5_hash"] = c[2], c[3]
            cached_hits.append(rec)
        else:
            to_process.append(rec)

    # A cached file must be re-checked if a new/changed file shares its size,
    # otherwise new duplicates of old files would be missed.
    fresh_sizes = {r["size_bytes"] for r in to_process}
    requeued = [r for r in cached_hits if r["size_bytes"] in fresh_sizes]
    results.extend(r for r in cached_hits if r["size_bytes"] not in fresh_sizes)
    to_process.extend(requeued)

    print(f"Reused from cache (unchanged): {len(results)}")
    print(f"Re-checked because a new file shares their size: {len(requeued)}")
    print(f"Needing hashing: {len(to_process)}")

    # STAGE 1: size grouping
    size_groups = defaultdict(list)
    for rec in to_process:
        size_groups[rec["size_bytes"]].append(rec)

    needs_partial, unique_size = [], 0
    for group in size_groups.values():
        if len(group) == 1:
            group[0]["hash_status"] = "unique_size"
            group[0]["md5_hash"] = ""
            results.append(group[0])
            unique_size += 1
        else:
            needs_partial.extend(group)
    print(f"Stage 1 - unique by size: {unique_size}")
    print(f"Stage 2 candidates: {len(needs_partial)}")

    # STAGE 2: partial hash
    partial_res, failed = run_threaded(
        needs_partial, lambda r: partial_hash(Path(r["full_path"]), r["size_bytes"]),
        "partial-hashed")
    for rec, msg in failed:
        fail(rec, msg)

    partial_groups = defaultdict(list)
    for rec in needs_partial:
        ph = partial_res.get(rec["full_path"])
        if ph is not None:
            partial_groups[(rec["size_bytes"], ph)].append(rec)

    needs_full, unique_partial = [], 0
    for group in partial_groups.values():
        if len(group) == 1:
            group[0]["hash_status"] = "unique_partial"
            group[0]["md5_hash"] = ""
            results.append(group[0])
            unique_partial += 1
        else:
            needs_full.extend(group)
    print(f"Stage 2 - unique by partial hash: {unique_partial}")
    print(f"Stage 3 candidates: {len(needs_full)}")

    # STAGE 3: full hash
    full_res, failed = run_threaded(
        needs_full, lambda r: full_md5(Path(r["full_path"])), "full-hashed")
    for rec, msg in failed:
        fail(rec, msg)
    for rec in needs_full:
        fh = full_res.get(rec["full_path"])
        if fh is not None:
            rec["hash_status"] = "verified"
            rec["md5_hash"] = fh
            results.append(rec)
    print(f"Stage 3 - fully hashed: {len(full_res)}")
    return results


def build_duplicates_report(df: pd.DataFrame, out_path: str):
    verified = df[df["hash_status"] == "verified"]
    if verified.empty:
        pd.DataFrame(columns=df.columns).to_csv(out_path, index=False)
        print("No confirmed duplicates found.")
        return
    counts = verified.groupby("md5_hash")["full_path"].transform("count")
    dups = verified[counts > 1].sort_values(["md5_hash", "full_path"])
    dups.to_csv(out_path, index=False)
    print(f"Confirmed exact duplicates: {len(dups)} files -> {out_path}")


def main():
    os.makedirs(OUTPUT_FOLDER, exist_ok=True)
    master_path = os.path.join(OUTPUT_FOLDER, MASTER_CSV)
    errors_path = os.path.join(OUTPUT_FOLDER, ERRORS_CSV)
    dup_path = os.path.join(OUTPUT_FOLDER, DUPLICATES_CSV)

    cache = load_cache(master_path)
    errors = []

    print("\n--- Walking folders (metadata only) ---")
    records = collect_metadata(errors)

    print("\n--- Hashing (size -> partial -> full) ---")
    results = hash_records(records, cache, errors)

    df = pd.DataFrame(results)
    df = df[INVENTORY_FIELDS] if not df.empty else pd.DataFrame(columns=INVENTORY_FIELDS)
    df.to_csv(master_path, index=False)
    print(f"\nMaster inventory saved: {master_path} ({len(df)} files)")

    if errors:
        pd.DataFrame(errors).to_csv(errors_path, index=False)
        print(f"Errors logged: {len(errors)} -> {errors_path}")

    build_duplicates_report(df, dup_path)


if __name__ == "__main__":
    main()

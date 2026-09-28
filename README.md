# Duplicate File Scanner

A fast, staged duplicate-file finder for large folders and network drives.
Instead of hashing every file, it narrows candidates in stages so most files
are never fully read.

## How it works

| Stage | What it does | Cost |
|-------|--------------|------|
| 1. Size grouping | A file with a unique size cannot be a duplicate, so it is skipped | Near zero |
| 2. Partial hash | For files sharing a size, hashes the first 4 KB and last 4 KB | Cheap |
| 3. Full hash | Only files matching on size and partial hash are fully hashed to confirm | Only true candidates |

Extras:

- **Skip-if-unchanged:** on later runs, files with the same size and modified
  time are reused from the previous inventory with no disk reads. If a new or
  changed file has the same size as a cached file, that cached file is
  re-checked, so new duplicates of old files are still found.
- **Multi-threading:** hashing is I/O-bound, so partial and full hashing run
  across several threads.
- **Error tracking:** unreadable files are listed in `errors.csv` and marked
  `error` in the master inventory. They are retried on the next run.

## Features

- Scans multiple root folders, including network shares
- Tracks Excel, Word, and PDF files (easy to change)
- Produces a master inventory, a confirmed-duplicates report, and an error log
- Incremental re-runs
- Read-only: never modifies or deletes your files

## Requirements

- Python 3.8+
- pandas

```bash
pip install -r requirements.txt
```

## Usage

1. Open `duplicate_scanner.py` and edit the config at the top:

```python
FOLDERS_TO_SCAN = [
    ("Folder A", r"C:\Users\YourName\Documents\FolderA"),
    ("Folder B", r"\\SERVER\Share\FolderB"),
]
OUTPUT_FOLDER = r"C:\Users\YourName\Documents\ScanResults"
```

2. Run:

```bash
python duplicate_scanner.py
```

3. Run it again later to process only new or changed files.

## Output

Written to `OUTPUT_FOLDER`:

| File | Contents |
|------|----------|
| `inventory_master.csv` | One row per file, with a `hash_status` column |
| `duplicates_report.csv` | Only files confirmed as exact duplicates (same MD5) |
| `errors.csv` | Files that could not be read |

### `hash_status` values

- `unique_size`: no other file has this size, so no hashing was needed
- `unique_partial`: shares a size with others, but its partial hash is unique
- `verified`: fully hashed; `md5_hash` holds the full-file hash
- `error`: could not be read (see `errors.csv`)

## Configuration

| Setting | Default | Meaning |
|---------|---------|---------|
| `ALL_TRACKED_EXT` | Excel, Word, PDF | File types to scan |
| `PARTIAL_CHUNK` | 4096 | Bytes read from start and end for the partial hash |
| `FULL_HASH_CHUNK` | 1 MB | Read size for full hashing |
| `MAX_WORKERS` | 8 | Threads for hashing |
| `PROGRESS_EVERY` | 500 | Progress print interval |

## Privacy note

The output CSVs contain real file paths and names. Do not commit or share
them. The included `.gitignore` excludes all `.csv` files.

## Limitations

- MD5 is used to detect identical content, not for security.
- Only byte-identical files are detected. Edited or re-saved copies are not.
- Cache matching uses size and modified time (to the minute). A file changed
  within the same minute without a size change would be treated as unchanged.

## License

MIT. See [LICENSE](LICENSE).

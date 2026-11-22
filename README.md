# PhotoSweep

Local, resumable desktop web app for cleaning large photo & video libraries (50,000+ files / 2 TB+) spread across multiple folders, external hard drives, and Windows OneDrive.

## What It Does

1. **Exact Duplicates (Photos, Videos & Optional Other Files across multiple drives)**
   - Groups by exact file size first (zero downloads for unique OneDrive cloud-only videos).
   - Uses fast 3-point partial SHA-256 hashing when sizes + normalized filenames match (e.g., `IMG_001.jpg` and `IMG_001 (1).jpg`), and full SHA-256 verification when filenames differ.
   - Optional **"Duplicate Other Files"** toggle detects duplicate non-media files (PDFs, docs, archives, etc.) matching exact byte size + extension + exact/similar name + 3-point partial hash.
   - Respects your **drive/folder priority order** so your preferred drive always keeps the original.

2. **Blurry Portrait Detection (Well-Lit Human Photos Only)**
   - Never flags photos without a detected human face.
   - Automatically skips dark/low-light photos (brightness + EXIF ISO) and slow-shutter motion shots.
   - Measures sharpness strictly inside detected face regions so portrait-mode background blur isn't misflagged.

3. **Similar Burst Culling (Keep #1 Best Shot)**
   - Clusters burst shots using EXIF timestamps + perceptual hashing (`pHash` + `dHash`).
   - Scores every photo in a burst by face sharpness, open eyes, smile, lighting, and resolution — keeping the single best shot.

4. **OneDrive & Small-SSD Safe**
   - Detects Windows OneDrive **Files On-Demand** cloud-only placeholders.
   - **Smart Cloud Mode:** analyzes cloud-only photos and immediately frees local SSD space (`attrib +U -P`), with a configurable `C:` drive free-space floor.
   - Auto-throttles when copying kept files *into* OneDrive so a small SSD never fills up.

5. **Safe Two-Step Workflow & Full Metadata Preservation**
   - **"Analyze Without Deletion"** runs the full analysis and shows a complete savings report before touching any file.
   - Choose **Move to Same-Drive Quarantine** (instant & reversible with **One-Click Undo**), **Permanent Delete**, or **Copy Kept Files** (`YYYY/MM` or original folders).
   - **Strictly preserves 100% of EXIF metadata** and exact Windows **Created & Modified timestamps**.
   - Separate local vs. cloud byte-based (`MB/s`) ETA tracking, **Retry Failed Files / Deletions**, downloadable failed-files `.csv` report, SQLite checkpointing in `%LOCALAPPDATA%\PhotoSweep`, and **Drive Disconnect Detection** with automatic **Reconnect & Resume**.

## Quick Start (Windows)

Double-click **`run.bat`** — it sets up the isolated virtual environment in `%LOCALAPPDATA%\PhotoSweep\venv` (outside OneDrive) and opens `http://127.0.0.1:8765` in your browser.

Or run manually:
```powershell
python -m venv "$env:LOCALAPPDATA\PhotoSweep\venv"
& "$env:LOCALAPPDATA\PhotoSweep\venv\Scripts\python.exe" -m pip install -r requirements.txt
& "$env:LOCALAPPDATA\PhotoSweep\venv\Scripts\python.exe" app.py
```

## Running Tests

```powershell
& "$env:LOCALAPPDATA\PhotoSweep\venv\Scripts\python.exe" -m unittest discover -s tests -v
```

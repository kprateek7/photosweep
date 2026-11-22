"""Configuration, presets, and constants for PhotoSweep."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "PhotoSweep"

# Store SQLite state & logs in LOCALAPPDATA (fast SSD, outside OneDrive sync!)
APP_DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_NAME
APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = APP_DATA_DIR / "photosweep.db"

PHOTO_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".heic",
    ".heif",
    ".bmp",
    ".tif",
    ".tiff",
}

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".avi",
    ".mkv",
    ".wmv",
    ".m4v",
    ".3gp",
    ".mts",
    ".m2ts",
    ".webm",
}

ALL_MEDIA_EXTENSIONS = PHOTO_EXTENSIONS | VIDEO_EXTENSIONS

# Folders automatically skipped during scan (system/recycle/quarantine)
SKIP_DIR_NAMES = {
    "$recycle.bin",
    "system volume information",
    ".git",
    "__pycache__",
    "node_modules",
    "_photosweep_quarantine",
}

# OS/temp files skipped when scanning non-media ("other") duplicate files
SKIP_OTHER_FILENAMES = {
    "thumbs.db",
    "desktop.ini",
    ".ds_store",
}

SKIP_OTHER_EXTENSIONS = {
    ".tmp",
    ".crdownload",
    ".part",
    ".lnk",
    ".sys",
    ".dll",
    ".ini",
}


@dataclass(frozen=True)
class PresetConfig:
    name: str
    label: str
    description: str
    # Face blur thresholds (computed on canonical 128x128 face ROI)
    blur_face_laplacian_min: float
    blur_face_tenengrad_min: float
    # Dark / motion protection
    min_brightness: float
    max_iso_for_blur: int
    max_exposure_seconds_for_blur: float
    # Similar burst clustering
    similar_phash_max_dist: int
    similar_dhash_max_dist: int
    burst_window_seconds: int
    # Cross-time near-identical visual match (e.g., stripped EXIF copies)
    exact_visual_phash_max_dist: int
    exact_visual_dhash_max_dist: int


PRESETS: dict[str, PresetConfig] = {
    "safe": PresetConfig(
        name="safe",
        label="Safe (Conservative)",
        description="Only flags obviously blurry portraits and tightly matching bursts.",
        blur_face_laplacian_min=45.0,
        blur_face_tenengrad_min=95.0,
        min_brightness=55.0,
        max_iso_for_blur=800,
        max_exposure_seconds_for_blur=1.0 / 60.0,
        similar_phash_max_dist=8,
        similar_dhash_max_dist=9,
        burst_window_seconds=180,
        exact_visual_phash_max_dist=4,
        exact_visual_dhash_max_dist=5,
    ),
    "balanced": PresetConfig(
        name="balanced",
        label="Balanced (Recommended)",
        description="Good balance for baby/friend bursts and clearly out-of-focus faces.",
        blur_face_laplacian_min=75.0,
        blur_face_tenengrad_min=145.0,
        min_brightness=45.0,
        max_iso_for_blur=1200,
        max_exposure_seconds_for_blur=1.0 / 30.0,
        similar_phash_max_dist=12,
        similar_dhash_max_dist=13,
        burst_window_seconds=600,
        exact_visual_phash_max_dist=6,
        exact_visual_dhash_max_dist=7,
    ),
    "aggressive": PresetConfig(
        name="aggressive",
        label="Aggressive (Maximum Space Saved)",
        description="Stricter sharpness requirement and wider burst grouping.",
        blur_face_laplacian_min=115.0,
        blur_face_tenengrad_min=210.0,
        min_brightness=35.0,
        max_iso_for_blur=2000,
        max_exposure_seconds_for_blur=1.0 / 15.0,
        similar_phash_max_dist=16,
        similar_dhash_max_dist=16,
        burst_window_seconds=1800,
        exact_visual_phash_max_dist=7,
        exact_visual_dhash_max_dist=8,
    ),
}

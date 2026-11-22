"""Photo & video hashing, EXIF parsing, face/blur analysis, and burst clustering."""

from __future__ import annotations

import difflib
import hashlib
import math
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import ExifTags, Image, ImageOps

try:
    cv2.setLogLevel(2)  # Silence noisy OpenCV 5.x INFO/WARN messages
except Exception:
    pass

from .config import PresetConfig, get_bundle_root

# Register HEIC/HEIF support if available
try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_SUPPORTED = True
except Exception:
    HEIF_SUPPORTED = False


# Pre-build EXIF tag name map
_EXIF_TAG_MAP = {v: k for k, v in ExifTags.TAGS.items()}
_TAG_DATETIME_ORIGINAL = _EXIF_TAG_MAP.get("DateTimeOriginal", 36867)
_TAG_DATETIME_DIGITIZED = _EXIF_TAG_MAP.get("DateTimeDigitized", 36868)
_TAG_DATETIME = _EXIF_TAG_MAP.get("DateTime", 306)
_TAG_EXIF_OFFSET = _EXIF_TAG_MAP.get("ExifOffset", 34665)
_TAG_ISO = _EXIF_TAG_MAP.get("ISOSpeedRatings", 34855)
_TAG_ISO_ALT = _EXIF_TAG_MAP.get("PhotographicSensitivity", 34855)
_TAG_EXPOSURE_TIME = _EXIF_TAG_MAP.get("ExposureTime", 33434)

# Regex to strip duplicate copy suffixes like "IMG_01 (1)", "IMG_01 - Copy", "IMG_01_copy2"
_COPY_SUFFIX_RE = re.compile(
    r"(?:[\s_-]*(?:\(\d+\)|\[\d+\]|copy(?:\s*\d+)?|\-\s*copy(?:\s*\(\d+\))?))+$",
    re.IGNORECASE,
)

# Thread-local OpenCV face detectors (not safe to share across threads)
_tls = threading.local()

YUNET_MODEL_PATH = (
    get_bundle_root() / "models" / "face_detection_yunet_2023mar.onnx"
)


def normalize_filename(filename: str) -> str:
    """Normalize a filename by lowercasing and stripping common duplicate suffixes like '(1)' or '- Copy'."""
    stem, ext = os.path.splitext(filename)
    cleaned = _COPY_SUFFIX_RE.sub("", stem).strip().lower()
    return f"{cleaned or stem.lower()}{ext.lower()}"


def are_names_similar(
    name_a: str,
    name_b: str,
    ext_a: str,
    ext_b: str,
    threshold: float = 0.85,
) -> bool:
    """
    Return True if two files share the same extension AND have exact or very similar
    normalized names (e.g., 'report.pdf' & 'report (1).pdf' or 'report_v1.pdf' & 'report_v1_copy.pdf').
    """
    if ext_a.lower() != ext_b.lower():
        return False
    norm_a = normalize_filename(name_a)
    norm_b = normalize_filename(name_b)
    if norm_a == norm_b:
        return True
    stem_a = os.path.splitext(norm_a)[0]
    stem_b = os.path.splitext(norm_b)[0]
    if not stem_a or not stem_b:
        return False
    # Check prefix/containment when one has a short suffix appended (e.g. "doc" & "doc_backup")
    short, long = (stem_a, stem_b) if len(stem_a) <= len(stem_b) else (stem_b, stem_a)
    if len(short) >= 4 and long.startswith(short) and (len(long) - len(short)) <= 8:
        return True
    return difflib.SequenceMatcher(None, stem_a, stem_b).ratio() >= threshold


def compute_partial_hash(path: str | Path, chunk_size: int = 65536) -> str:
    """
    Fast 3-point SHA-256 hash: first 64 KB + middle 64 KB + last 64 KB + file size.
    Takes ~2-5 ms even on multi-GB videos while catching virtually all distinct files.
    """
    p = Path(path)
    size = p.stat().st_size
    h = hashlib.sha256()
    h.update(str(size).encode("ascii"))
    with p.open("rb") as f:
        if size <= chunk_size * 3:
            h.update(f.read())
        else:
            h.update(f.read(chunk_size))
            f.seek(size // 2 - chunk_size // 2)
            h.update(f.read(chunk_size))
            f.seek(size - chunk_size)
            h.update(f.read(chunk_size))
    return h.hexdigest()


def compute_full_hash(path: str | Path, buf_size: int = 1024 * 1024) -> str:
    """Full streaming SHA-256 hash for 100% certainty when filenames differ."""
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        while True:
            chunk = f.read(buf_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _get_yunet_detector() -> Any:
    det = getattr(_tls, "yunet", None)
    if det is not None:
        return det
    if hasattr(cv2, "FaceDetectorYN") and YUNET_MODEL_PATH.is_file():
        det = cv2.FaceDetectorYN.create(
            str(YUNET_MODEL_PATH),
            "",
            (320, 320),
            score_threshold=0.68,
            nms_threshold=0.3,
            top_k=50,
        )
        _tls.yunet = det
        return det
    return None


def _get_haar_cascades() -> dict[str, Any] | None:
    if not hasattr(cv2, "CascadeClassifier"):
        return None
    cascades = getattr(_tls, "cascades", None)
    if cascades is not None:
        return cascades

    base = cv2.data.haarcascades
    cascades = {
        "face_alt2": cv2.CascadeClassifier(
            os.path.join(base, "haarcascade_frontalface_alt2.xml")
        ),
        "face_default": cv2.CascadeClassifier(
            os.path.join(base, "haarcascade_frontalface_default.xml")
        ),
        "eye_glass": cv2.CascadeClassifier(
            os.path.join(base, "haarcascade_eye_tree_eyeglasses.xml")
        ),
        "eye": cv2.CascadeClassifier(os.path.join(base, "haarcascade_eye.xml")),
        "smile": cv2.CascadeClassifier(os.path.join(base, "haarcascade_smile.xml")),
    }
    _tls.cascades = cascades
    return cascades


def _parse_exif_datetime(val: Any) -> float | None:
    if not val or not isinstance(val, str):
        return None
    s = val.strip().replace("\x00", "")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M"):
        try:
            return datetime.strptime(s[:19], fmt).timestamp()
        except ValueError:
            continue
    return None


def _to_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        if isinstance(val, (tuple, list)) and len(val) == 2 and val[1]:
            return float(val[0]) / float(val[1])
        return float(val)
    except Exception:
        return None


def _extract_exif(img: Image.Image) -> dict[str, Any]:
    exif_ts: float | None = None
    iso: int | None = None
    exposure: float | None = None

    try:
        exif = img.getexif()
        if exif:
            exif_ifd = {}
            try:
                exif_ifd = exif.get_ifd(_TAG_EXIF_OFFSET) or {}
            except Exception:
                exif_ifd = {}

            dt_raw = (
                exif_ifd.get(_TAG_DATETIME_ORIGINAL)
                or exif_ifd.get(_TAG_DATETIME_DIGITIZED)
                or exif.get(_TAG_DATETIME_ORIGINAL)
                or exif.get(_TAG_DATETIME)
            )
            exif_ts = _parse_exif_datetime(dt_raw)

            iso_raw = (
                exif_ifd.get(_TAG_ISO)
                or exif_ifd.get(_TAG_ISO_ALT)
                or exif.get(_TAG_ISO)
            )
            if isinstance(iso_raw, (tuple, list)) and iso_raw:
                iso_raw = iso_raw[0]
            if iso_raw is not None:
                try:
                    iso = int(iso_raw)
                except Exception:
                    iso = None

            exp_raw = exif_ifd.get(_TAG_EXPOSURE_TIME) or exif.get(_TAG_EXPOSURE_TIME)
            exposure = _to_float(exp_raw)
    except Exception:
        pass

    return {"exif_timestamp": exif_ts, "iso": iso, "exposure_time": exposure}


def compute_dhash(gray: np.ndarray) -> str:
    """Compute 64-bit difference hash as a 16-char hex string."""
    resized = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    diff = resized[:, 1:] > resized[:, :-1]
    bits = 0
    for b in diff.flatten():
        bits = (bits << 1) | int(bool(b))
    return f"{bits:016x}"


def compute_phash(gray: np.ndarray) -> str:
    """Compute 64-bit DCT perceptual hash as a 16-char hex string."""
    resized = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    dct = cv2.dct(resized)
    low_freq = dct[:8, :8].flatten()
    # Exclude DC coefficient at index 0 for median threshold
    med = float(np.median(low_freq[1:]))
    bits = 0
    for val in low_freq:
        bits = (bits << 1) | (1 if val > med else 0)
    return f"{bits:016x}"


def hamming_distance(hex_a: str, hex_b: str) -> int:
    return (int(hex_a, 16) ^ int(hex_b, 16)).bit_count()


def _sharpness_metrics(gray_patch: np.ndarray) -> tuple[float, float]:
    """Return (laplacian_var, tenengrad_mean) on a grayscale image patch."""
    lap_var = float(cv2.Laplacian(gray_patch, cv2.CV_64F).var())
    gx = cv2.Sobel(gray_patch, cv2.CV_64F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray_patch, cv2.CV_64F, 0, 1, ksize=3)
    tenengrad = float(np.mean(gx * gx + gy * gy)) ** 0.5
    return lap_var, tenengrad


def _detect_faces(
    bgr_det: np.ndarray, gray_det: np.ndarray
) -> list[dict[str, Any]]:
    """
    Detect human faces using OpenCV YuNet neural detector (with 5 facial landmarks),
    falling back to Haar cascades only if YuNet is unavailable.
    """
    h, w = gray_det.shape[:2]
    yunet = _get_yunet_detector()
    if yunet is not None:
        yunet.setInputSize((w, h))
        _, raw_faces = yunet.detect(bgr_det)
        if raw_faces is None or len(raw_faces) == 0:
            return []
        min_dim = max(20, int(min(h, w) * 0.035))
        results: list[dict[str, Any]] = []
        for row in raw_faces:
            fx, fy, fw, fh = int(row[0]), int(row[1]), int(row[2]), int(row[3])
            if fw < min_dim or fh < min_dim:
                continue
            results.append(
                {
                    "box": (fx, fy, fw, fh),
                    "right_eye": (float(row[4]), float(row[5])),
                    "left_eye": (float(row[6]), float(row[7])),
                    "nose": (float(row[8]), float(row[9])),
                    "right_mouth": (float(row[10]), float(row[11])),
                    "left_mouth": (float(row[12]), float(row[13])),
                    "score": float(row[14]),
                }
            )
        return results

    cascades = _get_haar_cascades()
    if cascades is None:
        return []
    min_dim = max(28, int(min(h, w) * 0.055))
    eq = cv2.equalizeHist(gray_det)
    faces = cascades["face_alt2"].detectMultiScale(
        eq, scaleFactor=1.1, minNeighbors=5, minSize=(min_dim, min_dim)
    )
    if len(faces) == 0:
        faces = cascades["face_default"].detectMultiScale(
            eq, scaleFactor=1.12, minNeighbors=6, minSize=(min_dim, min_dim)
        )
    if len(faces) == 0:
        return []
    return [
        {"box": (int(x), int(y), int(fw), int(fh)), "score": 0.8}
        for (x, y, fw, fh) in faces
    ]


def _eye_openness_from_landmark(
    gray_full: np.ndarray,
    cx: float,
    cy: float,
    patch_radius: int,
) -> float:
    """
    Estimate eye openness (0.0-1.0) around a YuNet eye landmark by comparing
    vertical iris/sclera contrast & edge energy in the local eye patch.
    """
    fh, fw = gray_full.shape[:2]
    r = max(6, patch_radius)
    x1 = max(0, int(cx - r * 1.3))
    x2 = min(fw, int(cx + r * 1.3))
    y1 = max(0, int(cy - r * 0.85))
    y2 = min(fh, int(cy + r * 0.85))
    patch = gray_full[y1:y2, x1:x2]
    if patch.size < 25:
        return 0.5
    p_norm = cv2.resize(patch, (32, 20), interpolation=cv2.INTER_AREA).astype(np.float32)
    # Open eyes exhibit higher local contrast (dark pupil vs brighter sclera/lid) and vertical gradient
    contrast = float(np.std(p_norm))
    gy = cv2.Sobel(p_norm, cv2.CV_32F, 0, 1, ksize=3)
    vert_energy = float(np.mean(np.abs(gy)))
    score = 0.5 * min(1.0, contrast / 38.0) + 0.5 * min(1.0, vert_energy / 35.0)
    return max(0.0, min(1.0, score))


def _score_face_features(
    gray_full: np.ndarray,
    faces: list[dict[str, Any]],
    scale_x: float,
    scale_y: float,
) -> dict[str, float]:
    """Compute canonical face sharpness, brightness, open-eyes ratio, and smile score."""
    cascades = _get_haar_cascades()
    fh_full, fw_full = gray_full.shape[:2]

    lap_scores: list[float] = []
    ten_scores: list[float] = []
    face_brightnesses: list[float] = []
    eyes_scores: list[float] = []
    smile_scores: list[float] = []

    for f in faces:
        x, y, w, h = f["box"]
        # Map coordinates back to high-res grayscale
        rx = max(0, int(x * scale_x))
        ry = max(0, int(y * scale_y))
        rw = min(fw_full - rx, max(16, int(w * scale_x)))
        rh = min(fh_full - ry, max(16, int(h * scale_y)))
        face_roi = gray_full[ry : ry + rh, rx : rx + rw]
        if face_roi.size == 0:
            continue

        face_brightnesses.append(float(np.mean(face_roi)))

        # Canonical 128x128 resize makes sharpness resolution-independent across cameras
        canon = cv2.resize(face_roi, (128, 128), interpolation=cv2.INTER_AREA)
        # Measure sharpness on the central 70% of the face (eyes/nose/mouth, excludes background hair)
        inner = canon[20:108, 20:108]
        lap, ten = _sharpness_metrics(inner)
        lap_scores.append(lap)
        ten_scores.append(ten)

        if "right_eye" in f and "left_eye" in f:
            re_x, re_y = f["right_eye"][0] * scale_x, f["right_eye"][1] * scale_y
            le_x, le_y = f["left_eye"][0] * scale_x, f["left_eye"][1] * scale_y
            iod = math.hypot(le_x - re_x, le_y - re_y)
            eye_r = max(6, int(iod * 0.24))
            e_right = _eye_openness_from_landmark(gray_full, re_x, re_y, eye_r)
            e_left = _eye_openness_from_landmark(gray_full, le_x, le_y, eye_r)
            eyes_scores.append(0.5 * (e_right + e_left))

            # Smile heuristic from YuNet mouth corners relative to inter-ocular distance
            rm_x, rm_y = f["right_mouth"][0] * scale_x, f["right_mouth"][1] * scale_y
            lm_x, lm_y = f["left_mouth"][0] * scale_x, f["left_mouth"][1] * scale_y
            mouth_w = math.hypot(lm_x - rm_x, lm_y - rm_y)
            ratio = (mouth_w / iod) if iod > 4.0 else 0.75
            # Neutral mouth width / IOD is ~0.72-0.78; wide smile is ~0.95-1.15+
            smile_val = max(0.0, min(1.0, (ratio - 0.72) / 0.35))
            smile_scores.append(smile_val)
        elif cascades is not None:
            upper = canon[10:76, :]
            eyes = cascades["eye_glass"].detectMultiScale(
                upper, scaleFactor=1.1, minNeighbors=3, minSize=(14, 14)
            )
            if len(eyes) == 0:
                eyes = cascades["eye"].detectMultiScale(
                    upper, scaleFactor=1.1, minNeighbors=3, minSize=(14, 14)
                )
            eyes_scores.append(min(len(eyes), 2) / 2.0)

            lower = canon[64:124, 12:116]
            smiles = cascades["smile"].detectMultiScale(
                lower, scaleFactor=1.5, minNeighbors=15, minSize=(20, 12)
            )
            smile_scores.append(1.0 if len(smiles) > 0 else 0.0)
        else:
            eyes_scores.append(0.5)
            smile_scores.append(0.5)

    if not lap_scores:
        return {
            "face_sharpness": 0.0,
            "face_tenengrad": 0.0,
            "face_brightness": 0.0,
            "eyes_open_ratio": 0.0,
            "smile_score": 0.0,
        }

    # For group shots, use the median face sharpness so one slightly turned person
    # doesn't ruin the whole group photo, while a completely blurry photo scores low everywhere.
    lap_scores.sort()
    ten_scores.sort()
    mid = len(lap_scores) // 2
    return {
        "face_sharpness": float(lap_scores[mid]),
        "face_tenengrad": float(ten_scores[mid]),
        "face_brightness": float(np.mean(face_brightnesses)),
        "eyes_open_ratio": float(np.mean(eyes_scores)),
        "smile_score": float(np.mean(smile_scores)),
    }


def _compute_quality_score(
    face_count: int,
    face_sharpness: float,
    whole_sharpness: float,
    eyes_open_ratio: float,
    smile_score: float,
    brightness: float,
    width: int,
    height: int,
    size_bytes: int,
) -> float:
    """Compute a 0-100 overall photo quality score used to pick the #1 photo in a burst."""
    # 1. Sharpness (0-45 pts) - saturating log curve
    sharp_val = face_sharpness if face_count > 0 else whole_sharpness
    sharp_pts = min(45.0, 45.0 * (math.log1p(max(0.0, sharp_val)) / math.log1p(600.0)))

    # 2. Faces: Eyes open (0-25 pts) + Smile (0-10 pts)
    if face_count > 0:
        eyes_pts = 25.0 * max(0.0, min(1.0, eyes_open_ratio))
        smile_pts = 10.0 * max(0.0, min(1.0, smile_score))
    else:
        # Neutral baseline when no human faces are in the scene
        eyes_pts = 12.5
        smile_pts = 5.0

    # 3. Exposure / brightness balance (0-10 pts; ideal range 85-165)
    if 85.0 <= brightness <= 165.0:
        exp_pts = 10.0
    elif brightness < 85.0:
        exp_pts = max(0.0, 10.0 * (brightness / 85.0))
    else:
        exp_pts = max(0.0, 10.0 * ((255.0 - brightness) / 90.0))

    # 4. Resolution & file size (0-10 pts; favors original camera photos over compressed copies)
    mp = (width * height) / 1_000_000.0
    res_pts = min(6.0, 6.0 * (math.log1p(max(0.0, mp)) / math.log1p(16.0)))
    mb = size_bytes / (1024.0 * 1024.0)
    size_pts = min(4.0, 4.0 * (math.log1p(max(0.0, mb)) / math.log1p(6.0)))

    return round(sharp_pts + eyes_pts + smile_pts + exp_pts + res_pts + size_pts, 2)


def analyze_photo(
    path: str | Path,
    size_bytes: int,
    preset: PresetConfig,
    enable_blur_detection: bool = True,
) -> dict[str, Any]:
    """
    Single-pass photo analysis:
    - Extracts EXIF (DateTimeOriginal, ISO, ExposureTime)
    - Computes dHash & pHash for burst clustering
    - Detects human faces (YuNet neural detector + 5 landmarks), open eyes, smiles, brightness, sharpness
    - Applies strict blur rule: ONLY flags well-lit photos with human faces
    """
    with Image.open(str(path)) as raw_img:
        exif_info = _extract_exif(raw_img)
        img = ImageOps.exif_transpose(raw_img)
        orig_w, orig_h = img.size

        if img.mode != "RGB":
            img = img.convert("RGB")

        # Keep up to 1600px copy for accurate face sharpness, and 640px copy for fast YuNet detection
        max_side = max(orig_w, orig_h)
        if max_side > 1600:
            scale_hi = 1600.0 / max_side
            img_hi = img.resize(
                (max(1, int(orig_w * scale_hi)), max(1, int(orig_h * scale_hi))),
                Image.Resampling.BILINEAR,
            )
        else:
            img_hi = img

        rgb_hi = np.asarray(img_hi, dtype=np.uint8)

    gray_hi = cv2.cvtColor(rgb_hi, cv2.COLOR_RGB2GRAY)
    hi_h, hi_w = gray_hi.shape[:2]

    det_max = 640
    if max(hi_w, hi_h) > det_max:
        det_scale = det_max / float(max(hi_w, hi_h))
        new_size = (max(1, int(hi_w * det_scale)), max(1, int(hi_h * det_scale)))
        bgr_det = cv2.resize(
            cv2.cvtColor(rgb_hi, cv2.COLOR_RGB2BGR),
            new_size,
            interpolation=cv2.INTER_AREA,
        )
        gray_det = cv2.resize(gray_hi, new_size, interpolation=cv2.INTER_AREA)
    else:
        bgr_det = cv2.cvtColor(rgb_hi, cv2.COLOR_RGB2BGR)
        gray_det = gray_hi

    det_h, det_w = gray_det.shape[:2]
    scale_x = hi_w / float(det_w)
    scale_y = hi_h / float(det_h)

    # Perceptual hashes
    dhash = compute_dhash(gray_det)
    phash = compute_phash(gray_det)

    # Whole-image brightness & sharpness (canonical 512px)
    brightness = float(np.mean(gray_det))
    norm_scale = 512.0 / float(max(det_w, det_h)) if max(det_w, det_h) > 0 else 1.0
    gray_512 = cv2.resize(
        gray_det,
        (max(32, int(det_w * norm_scale)), max(32, int(det_h * norm_scale))),
        interpolation=cv2.INTER_AREA,
    )
    whole_lap, _ = _sharpness_metrics(gray_512)

    # Face detection + per-face sharpness/eyes/smile
    faces = _detect_faces(bgr_det, gray_det)
    face_count = len(faces)
    if face_count > 0:
        feat = _score_face_features(gray_hi, faces, scale_x, scale_y)
        face_sharpness = feat["face_sharpness"]
        face_tenengrad = feat["face_tenengrad"]
        face_brightness = feat["face_brightness"]
        eyes_open_ratio = feat["eyes_open_ratio"]
        smile_score = feat["smile_score"]
    else:
        face_sharpness = 0.0
        face_tenengrad = 0.0
        face_brightness = brightness
        eyes_open_ratio = 0.0
        smile_score = 0.0

    # Dark & motion protection checks
    iso = exif_info["iso"]
    exposure = exif_info["exposure_time"]

    is_dark = (
        brightness < preset.min_brightness
        or (face_count > 0 and face_brightness < preset.min_brightness)
        or (iso is not None and iso > preset.max_iso_for_blur)
    )
    is_motion_risk = (
        exposure is not None and exposure > preset.max_exposure_seconds_for_blur
    )

    quality_score = _compute_quality_score(
        face_count=face_count,
        face_sharpness=face_sharpness,
        whole_sharpness=whole_lap,
        eyes_open_ratio=eyes_open_ratio,
        smile_score=smile_score,
        brightness=brightness,
        width=orig_w,
        height=orig_h,
        size_bytes=size_bytes,
    )

    # Strict Blur Decision:
    # (i) Must have at least 1 detected human face
    # (ii) Must NOT be dark and must NOT be a slow-shutter motion shot
    # (iii) Both Laplacian and Tenengrad sharpness on faces must fall below thresholds
    decision = "keep"
    decision_reason = ""
    if (
        enable_blur_detection
        and face_count >= 1
        and not is_dark
        and not is_motion_risk
        and face_sharpness < preset.blur_face_laplacian_min
        and face_tenengrad < preset.blur_face_tenengrad_min
    ):
        decision = "blurry"
        decision_reason = (
            f"Blurry human portrait (faces={face_count}, "
            f"sharpness={face_sharpness:.1f} < {preset.blur_face_laplacian_min:.0f}, "
            f"brightness={brightness:.0f})"
        )

    return {
        "analyzed": 1,
        "width": orig_w,
        "height": orig_h,
        "exif_timestamp": exif_info["exif_timestamp"],
        "iso": iso,
        "exposure_time": exposure,
        "dhash": dhash,
        "phash": phash,
        "brightness": round(brightness, 2),
        "face_count": face_count,
        "face_sharpness": round(face_sharpness, 2),
        "face_tenengrad": round(face_tenengrad, 2),
        "whole_sharpness": round(whole_lap, 2),
        "eyes_open_ratio": round(eyes_open_ratio, 3),
        "smile_score": round(smile_score, 3),
        "quality_score": quality_score,
        "is_dark": 1 if is_dark else 0,
        "is_motion_risk": 1 if is_motion_risk else 0,
        "decision": decision,
        "decision_reason": decision_reason,
    }


def cluster_similar_photos(
    photos: list[dict[str, Any]],
    preset: PresetConfig,
) -> list[tuple[dict[str, Any], int]]:
    """
    Fast 50K-scale burst & similar-photo clustering.
    Returns DB bulk-update list [(fields_dict, file_id), ...] flagging losers as 'similar_loser'
    while keeping the #1 highest-scoring shot in each cluster.
    """
    n = len(photos)
    if n < 2:
        return []

    # Pre-parse hashes as ints and timestamps for speed
    phash_ints = [int(p["phash"], 16) for p in photos]
    dhash_ints = [int(p["dhash"], 16) for p in photos]
    timestamps = [
        float(p["exif_timestamp"]) if p["exif_timestamp"] is not None else float(p["mtime"])
        for p in photos
    ]

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union_guarded(i: int, j: int, max_p: int, max_d: int) -> None:
        ri, rj = find(i), find(j)
        if ri == rj:
            return
        # Guard against long transitive chains merging unrelated photos
        if (phash_ints[ri] ^ phash_ints[rj]).bit_count() <= max_p + 2 and (
            dhash_ints[ri] ^ dhash_ints[rj]
        ).bit_count() <= max_d + 2:
            parent[rj] = ri

    # Pass 1: Sliding time window (photos are already sorted by timestamp ascending)
    window = float(preset.burst_window_seconds)
    max_p = preset.similar_phash_max_dist
    max_d = preset.similar_dhash_max_dist
    max_neighbors = 45

    for i in range(n):
        ti = timestamps[i]
        pi = phash_ints[i]
        di = dhash_ints[i]
        checked = 0
        for j in range(i + 1, n):
            if timestamps[j] - ti > window:
                break
            checked += 1
            if checked > max_neighbors:
                break
            if (pi ^ phash_ints[j]).bit_count() <= max_p and (
                di ^ dhash_ints[j]
            ).bit_count() <= max_d:
                union_guarded(i, j, max_p, max_d)

    # Pass 2: Time-independent near-identical match via 4x16-bit Multi-Index Hashing (MIH)
    # Catches visually identical copies across folders/WhatsApp where EXIF timestamps were stripped
    exact_p = preset.exact_visual_phash_max_dist
    exact_d = preset.exact_visual_dhash_max_dist
    buckets: list[dict[int, list[int]]] = [{}, {}, {}, {}]
    for idx, ph in enumerate(phash_ints):
        for band in range(4):
            key = (ph >> (band * 16)) & 0xFFFF
            buckets[band].setdefault(key, []).append(idx)

    for idx, ph in enumerate(phash_ints):
        di = dhash_ints[idx]
        seen_candidates: set[int] = set()
        for band in range(4):
            key = (ph >> (band * 16)) & 0xFFFF
            cand_list = buckets[band].get(key, ())
            if len(cand_list) > 250:
                # Skip overly dense buckets (e.g., solid black/white frames)
                continue
            for j in cand_list:
                if j <= idx or j in seen_candidates:
                    continue
                seen_candidates.add(j)
                if (ph ^ phash_ints[j]).bit_count() <= exact_p and (
                    di ^ dhash_ints[j]
                ).bit_count() <= exact_d:
                    union_guarded(idx, j, exact_p, exact_d)

    # Group clusters with size >= 2
    clusters: dict[int, list[int]] = {}
    for idx in range(n):
        root = find(idx)
        clusters.setdefault(root, []).append(idx)

    updates: list[tuple[dict[str, Any], int]] = []
    cluster_num = 0
    for members in clusters.values():
        if len(members) < 2:
            continue
        cluster_num += 1
        gid = f"burst_{cluster_num}"

        # Pick #1 best shot:
        # 1. Highest quality_score (sharpness + eyes open + smile + lighting + resolution)
        # 2. Higher source priority (lower source_priority integer)
        # 3. Larger file size (prefers original over re-compressed copy)
        # 4. Shorter filename (prefers IMG_001.jpg over IMG_001 (1).jpg)
        members.sort(
            key=lambda idx: (
                -float(photos[idx]["quality_score"]),
                int(photos[idx]["source_priority"]),
                -int(photos[idx]["size_bytes"]),
                len(photos[idx]["filename"]),
                int(photos[idx]["id"]),
            )
        )
        winner = photos[members[0]]
        winner_id = int(winner["id"])
        winner_score = float(winner["quality_score"])

        updates.append(
            (
                {
                    "group_id": gid,
                    "kept_file_id": winner_id,
                },
                winner_id,
            )
        )

        for loser_idx in members[1:]:
            loser = photos[loser_idx]
            loser_score = float(loser["quality_score"])
            updates.append(
                (
                    {
                        "decision": "similar_loser",
                        "decision_reason": (
                            f"Similar burst ({len(members)} photos) — kept '{winner['filename']}' "
                            f"(score {winner_score:.1f} vs {loser_score:.1f})"
                        ),
                        "group_id": gid,
                        "kept_file_id": winner_id,
                    },
                    int(loser["id"]),
                )
            )

    return updates

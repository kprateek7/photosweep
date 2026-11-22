"""End-to-end automated tests for PhotoSweep pipeline, metadata preservation, quarantine/undo, and API."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from photosweep.analyzer import normalize_filename
from photosweep.db import Database
from photosweep.engine import DriveDisconnectedError, JobEngine, ThroughputTracker


def _make_textured_image(
    width: int = 400,
    height: int = 300,
    seed: int = 42,
    blur_ksize: int = 0,
    brightness_scale: float = 1.0,
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = np.full((height, width, 3), 130, dtype=np.uint8)
    # Deterministic geometric pattern so pHash/dHash & sharpness are well-defined
    for i in range(0, width, 24):
        color = (int(rng.integers(40, 220)), int(rng.integers(40, 220)), int(rng.integers(40, 220)))
        cv2.rectangle(img, (i, 20), (i + 16, height - 20), color, -1)
    cv2.circle(img, (width // 2, height // 2), 65, (230, 190, 90), 5)
    cv2.putText(img, "TEST", (width // 2 - 55, height // 2 + 12), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (20, 20, 20), 3)
    if blur_ksize > 1:
        img = cv2.GaussianBlur(img, (blur_ksize, blur_ksize), 0)
    if brightness_scale != 1.0:
        img = np.clip(img.astype(np.float32) * brightness_scale, 0, 255).astype(np.uint8)
    return img


def _save_jpeg_with_exif(
    arr: np.ndarray,
    path: Path,
    dt_str: str = "2024:05:14 10:30:00",
    mtime: float | None = None,
) -> None:
    pil = Image.fromarray(cv2.cvtColor(arr, cv2.COLOR_BGR2RGB))
    exif = pil.getexif()
    exif[36867] = dt_str  # DateTimeOriginal
    exif[306] = dt_str    # DateTime
    pil.save(str(path), format="JPEG", quality=92, exif=exif)
    if mtime is not None:
        os.utime(str(path), (mtime, mtime))


class PhotoSweepPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="photosweep_test_"))
        self.db_path = self.tmp / "test.db"
        self.drive1 = self.tmp / "Drive1_Primary"
        self.drive2 = self.tmp / "Drive2_Backup"
        self.dest = self.tmp / "CleanMaster"
        self.drive1.mkdir()
        self.drive2.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _wait_idle(self, engine: JobEngine, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        while engine.is_busy():
            if time.time() > deadline:
                raise TimeoutError("Job engine did not finish in time")
            time.sleep(0.1)

    def test_filename_normalization(self) -> None:
        self.assertEqual(normalize_filename("IMG_001 (1).JPG"), "img_001.jpg")
        self.assertEqual(normalize_filename("IMG_001 - Copy (2).jpg"), "img_001.jpg")
        self.assertEqual(normalize_filename("Vacation_copy.MP4"), "vacation.mp4")

    def test_full_pipeline_analyze_quarantine_undo_and_copy(self) -> None:
        base_ts = 1715662800.0  # 2024-05-14

        # 1. Original sharp photo on Drive 1 + exact duplicates on Drive 2 (both "(1)" and renamed)
        sharp_img = _make_textured_image(seed=10, blur_ksize=0)
        orig_photo = self.drive1 / "IMG_1001.jpg"
        _save_jpeg_with_exif(sharp_img, orig_photo, "2024:05:14 10:30:00", mtime=base_ts)

        dup_same_name = self.drive2 / "IMG_1001 (1).jpg"
        dup_diff_name = self.drive2 / "RENAMED_BACKUP.jpg"
        shutil.copy2(orig_photo, dup_same_name)
        shutil.copy2(orig_photo, dup_diff_name)

        # 2. Exact duplicate video across drives (videos = exact dupes only)
        vid1 = self.drive1 / "CLIP_01.mp4"
        vid2 = self.drive2 / "CLIP_01 (1).mp4"
        vid_payload = os.urandom(300_000)
        vid1.write_bytes(vid_payload)
        vid2.write_bytes(vid_payload)

        # 3. Blurry NON-HUMAN landscape photo (MUST be kept — no face detected!)
        blurry_landscape = _make_textured_image(seed=99, blur_ksize=21)
        landscape_path = self.drive1 / "LANDSCAPE_BLUR.jpg"
        _save_jpeg_with_exif(blurry_landscape, landscape_path, "2024:06:01 12:00:00", mtime=base_ts + 86400 * 15)

        # 4. Dark low-light photo (MUST be kept!)
        dark_img = _make_textured_image(seed=77, blur_ksize=15, brightness_scale=0.15)
        dark_path = self.drive1 / "NIGHT_SHOT.jpg"
        _save_jpeg_with_exif(dark_img, dark_path, "2024:07:02 22:00:00", mtime=base_ts + 86400 * 45)

        # 5. Burst of 3 similar shots taken 2 seconds apart (one sharp, two softer -> keep sharpest #1!)
        burst_base = _make_textured_image(seed=202, blur_ksize=0)
        burst_soft1 = cv2.GaussianBlur(burst_base, (5, 5), 0)
        burst_soft2 = cv2.GaussianBlur(burst_base, (9, 9), 0)
        b1 = self.drive1 / "BURST_1.jpg"
        b2 = self.drive1 / "BURST_2.jpg"
        b3 = self.drive1 / "BURST_3.jpg"
        _save_jpeg_with_exif(burst_soft2, b1, "2024:08:10 15:00:00", mtime=base_ts + 86400 * 80)
        _save_jpeg_with_exif(burst_base, b2, "2024:08:10 15:00:02", mtime=base_ts + 86400 * 80 + 2)
        _save_jpeg_with_exif(burst_soft1, b3, "2024:08:10 15:00:04", mtime=base_ts + 86400 * 80 + 4)

        # Run Step 1: "Analyze Without Deletion"
        db = Database(self.db_path)
        engine = JobEngine(db)
        job = engine.start_analysis(
            {
                "sources": [str(self.drive1), str(self.drive2)],
                "preset": "balanced",
                "onedrive_mode": "smart_dehydrate",
            }
        )
        self._wait_idle(engine)

        job_state = db.get_job(job["id"])
        assert job_state is not None
        self.assertEqual(job_state["status"], "analyzed")

        # Verify ZERO files were deleted during analysis
        for p in (orig_photo, dup_same_name, dup_diff_name, vid1, vid2, landscape_path, dark_path, b1, b2, b3):
            self.assertTrue(p.exists(), f"Analysis must not delete {p}")

        summary = job_state["summary"]
        # Exact duplicates: 2 photo copies + 1 video copy = 3
        self.assertEqual(summary["categories"]["exact_duplicate"]["count"], 3)
        self.assertEqual(summary["categories"]["exact_duplicate"]["photos"], 2)
        self.assertEqual(summary["categories"]["exact_duplicate"]["videos"], 1)

        # Blurry: landscape & dark shot must NOT be flagged blurry
        self.assertEqual(summary["categories"]["blurry"]["count"], 0)

        # Similar burst: 2 losers out of the 3 burst shots, with sharp BURST_2 kept!
        self.assertEqual(summary["categories"]["similar_loser"]["count"], 2)
        kept_paths = {f["path"] for f in db.get_kept_files(job["id"])}
        self.assertIn(str(orig_photo), kept_paths)
        self.assertIn(str(vid1), kept_paths)
        self.assertIn(str(landscape_path), kept_paths)
        self.assertIn(str(dark_path), kept_paths)
        self.assertIn(str(b2), kept_paths)  # Sharpest burst photo won!
        self.assertNotIn(str(dup_same_name), kept_paths)
        self.assertNotIn(str(vid2), kept_paths)

        # Run Step 2: Quarantine flagged files & verify Undo restores them!
        engine.execute_action(job["id"], {"mode": "quarantine"})
        self._wait_idle(engine)
        self.assertFalse(dup_same_name.exists())
        self.assertFalse(vid2.exists())
        self.assertTrue(orig_photo.exists())

        engine.execute_action(job["id"], {"mode": "undo_quarantine"})
        self._wait_idle(engine)
        self.assertTrue(dup_same_name.exists())
        self.assertTrue(vid2.exists())

        # Clean up quarantine directory created on drive root during test
        q_root = Path(os.path.splitdrive(str(self.tmp))[0] + os.sep) / "_PhotoSweep_Quarantine" / job["id"]
        if q_root.exists():
            shutil.rmtree(q_root, ignore_errors=True)

        # Run Step 3: Copy Kept Files to new destination (YYYY/MM) and verify EXIF + Timestamps!
        orig_stat = orig_photo.stat()
        engine.execute_action(
            job["id"],
            {"mode": "copy_kept", "destination": str(self.dest), "folder_layout": "date_ym"},
        )
        self._wait_idle(engine)

        copied_orig = self.dest / "2024" / "05" / "IMG_1001.jpg"
        self.assertTrue(copied_orig.exists(), "Kept photo should be organized into 2024/05/")
        copied_stat = copied_orig.stat()
        self.assertAlmostEqual(copied_stat.st_mtime, orig_stat.st_mtime, delta=2.0)
        self.assertAlmostEqual(copied_stat.st_ctime, orig_stat.st_ctime, delta=2.0)

        with Image.open(copied_orig) as im:
            exif = im.getexif()
            self.assertEqual(exif.get(36867), "2024:05:14 10:30:00")

    def test_drive_disconnect_and_resume(self) -> None:
        db = Database(self.db_path)
        engine = JobEngine(db)
        temp_drive = self.tmp / "RemovableDrive"
        temp_drive.mkdir()
        _save_jpeg_with_exif(_make_textured_image(seed=1), temp_drive / "A.jpg")

        job = engine.start_analysis({"sources": [str(temp_drive)], "preset": "balanced"})
        self._wait_idle(engine)

        # Simulate drive disappearing before resume
        shutil.rmtree(temp_drive)
        db.update_job(job["id"], status="drive_disconnected", disconnected_path=str(temp_drive))
        with self.assertRaises(DriveDisconnectedError):
            engine.resume_analysis(job["id"])

        # Reconnect drive & resume
        temp_drive.mkdir()
        resumed = engine.resume_analysis(job["id"])
        self._wait_idle(engine)
        final_job = db.get_job(resumed["id"])
        assert final_job is not None
        self.assertEqual(final_job["status"], "analyzed")

    def test_throughput_tracker_separate_local_cloud_eta(self) -> None:
        t = ThroughputTracker(alpha=1.0)
        # Local at 100 MB/s, Cloud at 10 MB/s
        mb = 1024 * 1024
        t.record(100 * mb, 1.0, is_cloud=False)
        t.record(10 * mb, 1.0, is_cloud=True)
        # 200 MB local (2s) + 50 MB cloud (5s) = 7s
        eta = t.estimate_eta(200 * mb, 50 * mb)
        self.assertEqual(eta, 7)

    def test_partial_delete_failure_retry_and_all_deleted_summary(self) -> None:
        img = _make_textured_image(seed=33)
        orig = self.drive1 / "A.jpg"
        dup = self.drive2 / "A (1).jpg"
        _save_jpeg_with_exif(img, orig)
        shutil.copy2(orig, dup)

        db = Database(self.db_path)
        engine = JobEngine(db)
        job = engine.start_analysis({"sources": [str(self.drive1), str(self.drive2)]})
        self._wait_idle(engine)

        # Simulate a file that failed deletion on first attempt
        flagged = db.get_flagged_files(job["id"])
        self.assertEqual(len(flagged), 1)
        db.update_file(
            flagged[0]["id"],
            action_status="failed",
            action_error="Simulated OneDrive lock",
        )
        s1 = db.compute_summary(job["id"])
        self.assertTrue(s1["has_failures"])
        self.assertFalse(s1["all_flagged_deleted"])
        self.assertEqual(s1["actioned"]["failed_flagged"], 1)
        self.assertEqual(len(db.get_failed_files(job["id"])), 1)

        # Retry failed deletions -> should delete cleanly and flip all_flagged_deleted = True
        engine.execute_action(
            job["id"],
            {"mode": "delete", "retry_failed": True},
        )
        self._wait_idle(engine)

        s2 = db.compute_summary(job["id"])
        self.assertFalse(dup.exists())
        self.assertFalse(s2["has_failures"])
        self.assertTrue(s2["all_flagged_deleted"])
        self.assertEqual(s2["actioned"]["deleted"], 1)
        self.assertEqual(s2["actioned"]["failed_flagged"], 0)

    def test_duplicate_other_files_option(self) -> None:
        payload_a = b"%PDF-1.4 " + os.urandom(50_000)
        payload_b = b"%PDF-1.4 " + os.urandom(50_000)  # Exact same size, different content

        f_orig = self.drive1 / "Tax_2024.pdf"
        f_dup1 = self.drive2 / "Tax_2024 (1).pdf"
        f_dup2 = self.drive2 / "Tax_2024_old.pdf"       # Similar name + same content -> duplicate
        f_diff_name = self.drive2 / "CompletelyUnrelated.pdf"  # Different name -> keep
        f_diff_content = self.drive2 / "Tax_2024_v2.pdf"       # Similar name, different bytes -> keep
        f_diff_ext = self.drive2 / "Tax_2024.docx"             # Different extension -> keep

        f_orig.write_bytes(payload_a)
        f_dup1.write_bytes(payload_a)
        f_dup2.write_bytes(payload_a)
        f_diff_name.write_bytes(payload_a)
        f_diff_content.write_bytes(payload_b)
        f_diff_ext.write_bytes(payload_a)

        db = Database(self.db_path)
        engine = JobEngine(db)

        # 1. Disabled by default -> 0 files indexed/flagged
        job_off = engine.start_analysis(
            {"sources": [str(self.drive1), str(self.drive2)], "enable_other_duplicates": False}
        )
        self._wait_idle(engine)
        s_off = db.get_job(job_off["id"])["summary"]  # type: ignore[index]
        self.assertEqual(s_off["total_files"], 0)

        # 2. Enabled -> only f_dup1 & f_dup2 flagged as exact_duplicate
        job_on = engine.start_analysis(
            {"sources": [str(self.drive1), str(self.drive2)], "enable_other_duplicates": True}
        )
        self._wait_idle(engine)
        s_on = db.get_job(job_on["id"])["summary"]  # type: ignore[index]
        self.assertEqual(s_on["categories"]["exact_duplicate"]["count"], 2)
        self.assertEqual(s_on["categories"]["exact_duplicate"]["other"], 2)

        flagged_paths = {f["path"] for f in db.get_flagged_files(job_on["id"])}
        self.assertEqual(flagged_paths, {str(f_dup1), str(f_dup2)})

        # Delete flagged -> duplicates removed, all safe files intact
        engine.execute_action(job_on["id"], {"mode": "delete"})
        self._wait_idle(engine)
        self.assertFalse(f_dup1.exists())
        self.assertFalse(f_dup2.exists())
        for safe_f in (f_orig, f_diff_name, f_diff_content, f_diff_ext):
            self.assertTrue(safe_f.exists(), f"Should not delete {safe_f}")

    def test_retry_failed_download_or_analysis(self) -> None:
        good_img = self.drive1 / "GOOD.jpg"
        bad_img = self.drive1 / "TEMP_UNREADABLE.jpg"
        _save_jpeg_with_exif(_make_textured_image(seed=11), good_img)
        bad_img.write_bytes(b"not-a-valid-jpeg-yet")  # Simulates failed/incomplete cloud download

        db = Database(self.db_path)
        engine = JobEngine(db)
        job = engine.start_analysis({"sources": [str(self.drive1)]})
        self._wait_idle(engine)

        s1 = db.get_job(job["id"])["summary"]  # type: ignore[index]
        self.assertEqual(s1["actioned"]["download_errors"], 1)
        self.assertTrue(s1["has_failures"])

        # Fix the file (simulating cloud file now downloadable) and retry only failed files
        _save_jpeg_with_exif(_make_textured_image(seed=12), bad_img)
        engine.retry_failed_analysis(job["id"])
        self._wait_idle(engine)

        s2 = db.get_job(job["id"])["summary"]  # type: ignore[index]
        self.assertEqual(s2["actioned"]["download_errors"], 0)
        self.assertFalse(s2["has_failures"])
        self.assertEqual(s2["kept_count"], 2)


if __name__ == "__main__":
    unittest.main()

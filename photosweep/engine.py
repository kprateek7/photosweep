"""Resumable multi-stage analysis & execution engine with drive-disconnect and OneDrive SSD guards."""

from __future__ import annotations

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from .analyzer import (
    analyze_photo,
    are_names_similar,
    cluster_similar_photos,
    compute_full_hash,
    compute_partial_hash,
    normalize_filename,
)
from .config import (
    ALL_MEDIA_EXTENSIONS,
    PHOTO_EXTENSIONS,
    PRESETS,
    SKIP_DIR_NAMES,
    SKIP_OTHER_EXTENSIONS,
    SKIP_OTHER_FILENAMES,
    VIDEO_EXTENSIONS,
)
from .db import Database
from .win_utils import (
    copy_file_preserve_all_metadata,
    dehydrate_onedrive_file,
    delete_file_with_retry,
    get_drive_free_gb,
    get_drive_root,
    is_cloud_only_file,
    is_onedrive_path,
    is_path_accessible,
    move_file_preserve_all_metadata,
    prevent_sleep,
)


class DriveDisconnectedError(Exception):
    def __init__(self, missing_path: str) -> None:
        super().__init__(f"Drive or folder disconnected: {missing_path}")
        self.missing_path = missing_path


class JobPausedError(Exception):
    pass


class JobCancelledError(Exception):
    pass


class LowDiskSpaceError(Exception):
    def __init__(self, drive_root: str, free_gb: float, min_gb: float) -> None:
        super().__init__(
            f"Low disk space on {drive_root} ({free_gb:.1f} GB free, minimum {min_gb:.1f} GB required)"
        )
        self.drive_root = drive_root
        self.free_gb = free_gb


class ThroughputTracker:
    """
    Tracks smoothed (EWMA) byte throughput separately for local disk vs. OneDrive
    cloud-only files, giving accurate byte-weighted ETAs on mixed/cloud libraries.
    """

    def __init__(self, alpha: float = 0.25) -> None:
        self.alpha = alpha
        self.local_bps: float | None = None
        self.cloud_bps: float | None = None
        self.overall_bps: float | None = None
        self.active_source: str = "local"
        self.total_bytes_done: int = 0
        self.started_at: float = time.time()

    def record(self, num_bytes: int, duration_sec: float, is_cloud: bool) -> None:
        if duration_sec <= 0.0005 or num_bytes <= 0:
            return
        sample_bps = num_bytes / duration_sec
        self.total_bytes_done += num_bytes
        self.active_source = "cloud" if is_cloud else "local"

        if is_cloud:
            self.cloud_bps = (
                sample_bps
                if self.cloud_bps is None
                else (self.alpha * sample_bps + (1.0 - self.alpha) * self.cloud_bps)
            )
        else:
            self.local_bps = (
                sample_bps
                if self.local_bps is None
                else (self.alpha * sample_bps + (1.0 - self.alpha) * self.local_bps)
            )

        self.overall_bps = (
            sample_bps
            if self.overall_bps is None
            else (self.alpha * sample_bps + (1.0 - self.alpha) * self.overall_bps)
        )

    def estimate_eta(self, rem_local_bytes: int, rem_cloud_bytes: int) -> int | None:
        total_rem = max(0, rem_local_bytes) + max(0, rem_cloud_bytes)
        if total_rem == 0:
            return 0
        fallback = self.overall_bps
        if not fallback or fallback <= 1.0:
            elapsed = max(0.001, time.time() - self.started_at)
            fallback = self.total_bytes_done / elapsed if self.total_bytes_done > 0 else None
        if not fallback or fallback <= 1.0:
            return None

        local_rate = self.local_bps or fallback
        cloud_rate = self.cloud_bps or fallback
        secs = (max(0, rem_local_bytes) / local_rate) + (max(0, rem_cloud_bytes) / cloud_rate)
        return max(1, int(round(secs)))

    def current_mb_per_sec(self) -> float:
        rate = (
            self.cloud_bps
            if self.active_source == "cloud" and self.cloud_bps
            else self.local_bps or self.overall_bps or 0.0
        )
        return round(rate / (1024.0 * 1024.0), 2)


class JobEngine:
    """Manages background analysis and file execution jobs with full SQLite checkpointing."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._lock = threading.Lock()
        self._active_job_id: str | None = None
        self._thread: threading.Thread | None = None
        self._pause_event = threading.Event()
        self._cancel_event = threading.Event()
        self._runtime_stats: dict[str, Any] = {
            "started_at": 0.0,
            "items_done_in_phase": 0,
            "items_total_in_phase": 0,
            "rate_per_sec": 0.0,
            "eta_seconds": None,
            "action_mode": None,
        }

    # ---------------- Public Controls ----------------

    def is_busy(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def get_active_job_id(self) -> str | None:
        with self._lock:
            return self._active_job_id

    def get_runtime_stats(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._runtime_stats)

    def start_analysis(self, config: dict[str, Any]) -> dict[str, Any]:
        if self.is_busy():
            raise RuntimeError("Another job is currently running. Pause or cancel it first.")

        sources = config.get("sources") or []
        if not sources:
            raise ValueError("Please select at least one folder or drive to analyze.")

        # Validate accessibility upfront
        cleaned_sources: list[str] = []
        seen: set[str] = set()
        for s in sources:
            abs_p = os.path.abspath(str(s).strip())
            if not is_path_accessible(abs_p):
                raise ValueError(f"Folder or drive is not accessible: {abs_p}")
            norm = os.path.normcase(abs_p)
            if norm not in seen:
                seen.add(norm)
                cleaned_sources.append(abs_p)

        preset_name = config.get("preset", "balanced")
        if preset_name not in PRESETS:
            preset_name = "balanced"

        normalized_config = {
            "sources": cleaned_sources,
            "preset": preset_name,
            "onedrive_mode": config.get("onedrive_mode", "smart_dehydrate"),
            "min_ssd_free_gb": float(config.get("min_ssd_free_gb", 15.0)),
            "enable_duplicates": bool(config.get("enable_duplicates", True)),
            "enable_other_duplicates": bool(config.get("enable_other_duplicates", False)),
            "enable_blur": bool(config.get("enable_blur", True)),
            "enable_similar": bool(config.get("enable_similar", True)),
            "video_duplicates_only": True,
        }

        job_id = f"job_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        job = self.db.create_job(job_id, normalized_config)
        self._launch_worker(job_id, self._run_analysis_pipeline, (job_id,))
        return job

    def resume_analysis(self, job_id: str) -> dict[str, Any]:
        if self.is_busy():
            raise RuntimeError("A job is already running.")
        job = self.db.get_job(job_id)
        if not job:
            raise ValueError("Job not found.")

        # Verify all source roots are accessible again before resuming
        for src in job["config"].get("sources", []):
            if not is_path_accessible(src):
                self.db.update_job(
                    job_id,
                    status="drive_disconnected",
                    disconnected_path=src,
                    status_message=f"Still waiting for {src} to be reconnected.",
                )
                raise DriveDisconnectedError(src)

        self.db.update_job(
            job_id,
            disconnected_path="",
            error_message="",
            status_message="Resuming analysis...",
        )
        self._launch_worker(job_id, self._run_analysis_pipeline, (job_id,))
        return self.db.get_job(job_id)  # type: ignore[return-value]

    def retry_failed_analysis(self, job_id: str) -> dict[str, Any]:
        """Retry only files that previously failed to download/read during analysis."""
        if self.is_busy():
            raise RuntimeError("A job is already running.")
        job = self.db.get_job(job_id)
        if not job:
            raise ValueError("Job not found.")

        for src in job["config"].get("sources", []):
            if not is_path_accessible(src):
                self.db.update_job(
                    job_id,
                    status="drive_disconnected",
                    disconnected_path=src,
                    status_message=f"Still waiting for {src} to be reconnected.",
                )
                raise DriveDisconnectedError(src)

        count = self.db.reset_error_files_for_retry(job_id)
        if count == 0:
            raise ValueError("No failed downloads or unreadable files left to retry.")

        # Synchronously mark active before launching thread so UI immediately switches to progress bar
        self.db.update_job(
            job_id,
            status="analyzing",
            phase="analyzing",
            disconnected_path="",
            error_message="",
            processed_files=0,
            total_files=count,
            current_file="",
            status_message=f"Retrying {count:,} file(s) that previously failed to download or read...",
        )
        self._launch_worker(job_id, self._run_analysis_pipeline, (job_id,))
        return self.db.get_job(job_id)  # type: ignore[return-value]

    def execute_action(self, job_id: str, options: dict[str, Any]) -> dict[str, Any]:
        if self.is_busy():
            raise RuntimeError("A job is already running.")
        job = self.db.get_job(job_id)
        if not job:
            raise ValueError("Job not found.")

        mode = options.get("mode")
        if mode not in ("quarantine", "delete", "copy_kept", "undo_quarantine"):
            raise ValueError(f"Unsupported action mode: {mode}")

        if mode == "copy_kept":
            dest = str(options.get("destination", "")).strip()
            if not dest:
                raise ValueError("Destination folder is required for copying kept files.")
            dest_root = get_drive_root(dest)
            if not is_path_accessible(dest_root):
                raise ValueError(f"Destination drive is not accessible: {dest_root}")

        phase_map = {
            "quarantine": ("quarantining", "Preparing to move flagged files to quarantine..."),
            "delete": ("deleting", "Preparing to permanently delete flagged files..."),
            "copy_kept": ("copying", "Preparing to copy kept files..."),
            "undo_quarantine": ("restoring", "Preparing to restore quarantined files..."),
        }
        phase_name, init_msg = phase_map[mode]
        # Synchronously mark as executing BEFORE launching thread so UI immediately shows progress bar
        self.db.update_job(
            job_id,
            status="executing",
            phase=phase_name,
            total_files=0,
            processed_files=0,
            current_file="",
            status_message=init_msg,
        )

        self._launch_worker(job_id, self._run_execution_pipeline, (job_id, options))
        return self.db.get_job(job_id)  # type: ignore[return-value]

    def pause(self) -> None:
        self._pause_event.set()

    def cancel(self) -> None:
        self._cancel_event.set()

    # ---------------- Internal Worker Plumbing ----------------

    def _launch_worker(self, job_id: str, target: Any, args: tuple[Any, ...]) -> None:
        with self._lock:
            self._pause_event.clear()
            self._cancel_event.clear()
            self._active_job_id = job_id
            self._runtime_stats = {
                "started_at": time.time(),
                "items_done_in_phase": 0,
                "items_total_in_phase": 0,
                "rate_per_sec": 0.0,
                "eta_seconds": None,
                "action_mode": None,
            }
            self._thread = threading.Thread(
                target=self._worker_wrapper,
                args=(job_id, target, args),
                daemon=True,
                name=f"PhotoSweep-{job_id}",
            )
            self._thread.start()

    def _worker_wrapper(self, job_id: str, target: Any, args: tuple[Any, ...]) -> None:
        prevent_sleep(True)
        try:
            target(*args)
        except DriveDisconnectedError as exc:
            self.db.update_job(
                job_id,
                status="drive_disconnected",
                disconnected_path=exc.missing_path,
                status_message=f"Drive disconnected: {exc.missing_path}. Reconnect it and click Resume.",
            )
        except LowDiskSpaceError as exc:
            self.db.update_job(
                job_id,
                status="paused",
                status_message=str(exc) + " Free up space or adjust limit, then click Resume.",
            )
        except JobPausedError:
            self.db.update_job(
                job_id,
                status="paused",
                status_message="Paused cleanly. Progress is saved — click Resume anytime.",
            )
        except JobCancelledError:
            self.db.update_job(
                job_id,
                status="cancelled",
                status_message="Job cancelled by user.",
            )
        except Exception as exc:
            self.db.update_job(
                job_id,
                status="error",
                error_message=str(exc),
                status_message=f"Error: {exc}",
            )
        finally:
            prevent_sleep(False)
            with self._lock:
                self._active_job_id = None

    def _check_control(self, roots_to_verify: list[str] | None = None) -> None:
        if self._cancel_event.is_set():
            raise JobCancelledError()
        if self._pause_event.is_set():
            raise JobPausedError()
        if roots_to_verify:
            for r in roots_to_verify:
                if not is_path_accessible(r):
                    raise DriveDisconnectedError(r)

    def _reset_phase_metrics(self, total: int = 0, action_mode: str | None = None) -> None:
        with self._lock:
            self._runtime_stats = {
                "started_at": time.time(),
                "items_done_in_phase": 0,
                "items_total_in_phase": total,
                "rate_per_sec": 0.0,
                "mb_per_sec": 0.0,
                "eta_seconds": None,
                "remaining_local": 0,
                "remaining_cloud": 0,
                "remaining_local_mb": 0.0,
                "remaining_cloud_mb": 0.0,
                "active_source_type": "local",
                "action_mode": action_mode,
            }

    def _update_phase_metrics(
        self,
        done: int,
        total: int,
        tracker: ThroughputTracker | None = None,
        rem_work: dict[str, int] | None = None,
    ) -> None:
        now = time.time()
        with self._lock:
            elapsed = max(0.001, now - self._runtime_stats["started_at"])
            rate = done / elapsed
            remaining = max(0, total - done)

            eta: int | None
            mb_s = 0.0
            active_src = "local"
            if tracker is not None and rem_work is not None:
                eta = tracker.estimate_eta(
                    rem_work.get("local_bytes", 0), rem_work.get("cloud_bytes", 0)
                )
                mb_s = tracker.current_mb_per_sec()
                active_src = tracker.active_source
            else:
                eta = int(remaining / rate) if rate > 0.05 and total > 0 else None

            self._runtime_stats["items_done_in_phase"] = done
            self._runtime_stats["items_total_in_phase"] = total
            self._runtime_stats["rate_per_sec"] = round(rate, 2)
            self._runtime_stats["mb_per_sec"] = mb_s
            self._runtime_stats["eta_seconds"] = eta
            self._runtime_stats["active_source_type"] = active_src
            if rem_work is not None:
                self._runtime_stats["remaining_local"] = int(rem_work.get("local_cnt", 0))
                self._runtime_stats["remaining_cloud"] = int(rem_work.get("cloud_cnt", 0))
                self._runtime_stats["remaining_local_mb"] = round(
                    rem_work.get("local_bytes", 0) / (1024.0 * 1024.0), 1
                )
                self._runtime_stats["remaining_cloud_mb"] = round(
                    rem_work.get("cloud_bytes", 0) / (1024.0 * 1024.0), 1
                )

    def _guard_ssd_space(self, file_path: str, min_free_gb: float) -> None:
        drive_root = get_drive_root(file_path)
        free_gb = get_drive_free_gb(drive_root)
        if free_gb < min_free_gb:
            raise LowDiskSpaceError(drive_root, free_gb, min_free_gb)

    # ---------------- Analysis Pipeline ("Analyze Without Deletion") ----------------

    def _run_analysis_pipeline(self, job_id: str) -> None:
        job = self.db.get_job(job_id)
        assert job is not None
        cfg = job["config"]
        sources: list[str] = cfg["sources"]
        preset = PRESETS[cfg["preset"]]
        onedrive_mode: str = cfg.get("onedrive_mode", "smart_dehydrate")
        min_ssd_free_gb: float = float(cfg.get("min_ssd_free_gb", 15.0))

        include_other = bool(cfg.get("enable_other_duplicates", False))
        enable_media_dupes = bool(cfg.get("enable_duplicates", True))

        # Phase 1: Scan folders & index metadata (ZERO cloud downloads!)
        if not job["scan_completed"]:
            self._phase_scan(job_id, sources, include_other_files=include_other)
            self.db.update_job(job_id, scan_completed=1)

        # Phase 2: Exact Duplicates (Photos, Videos, and optionally Other Files)
        job = self.db.get_job(job_id)
        assert job is not None
        if (enable_media_dupes or include_other) and not job["dedup_completed"]:
            self._phase_exact_duplicates(
                job_id,
                sources,
                onedrive_mode,
                min_ssd_free_gb,
                enable_media_dupes=enable_media_dupes,
                enable_other_dupes=include_other,
            )
            self.db.update_job(job_id, dedup_completed=1)
        elif not (enable_media_dupes or include_other):
            self.db.update_job(job_id, dedup_completed=1)

        # Phase 3: Single-pass Photo Analysis (EXIF + dHash/pHash + Face/Eye/Smile/Blur)
        job = self.db.get_job(job_id)
        assert job is not None
        if (cfg.get("enable_blur", True) or cfg.get("enable_similar", True)) and not job[
            "analysis_completed"
        ]:
            self._phase_analyze_photos(
                job_id=job_id,
                sources=sources,
                preset=preset,
                enable_blur=bool(cfg.get("enable_blur", True)),
                onedrive_mode=onedrive_mode,
                min_ssd_free_gb=min_ssd_free_gb,
            )
            self.db.update_job(job_id, analysis_completed=1)
        else:
            self.db.update_job(job_id, analysis_completed=1)

        # Phase 4: Burst / Similar Clustering (Pure SQLite + RAM — takes seconds!)
        job = self.db.get_job(job_id)
        assert job is not None
        if cfg.get("enable_similar", True) and not job["cluster_completed"]:
            self._phase_cluster_similar(job_id, preset)
            self.db.update_job(job_id, cluster_completed=1)
        else:
            self.db.update_job(job_id, cluster_completed=1)

        summary = self.db.compute_summary(job_id)
        self.db.update_job(
            job_id,
            status="analyzed",
            phase="analyzed",
            current_file="",
            summary_json=summary,
            status_message=(
                f"Analysis complete (no files deleted). "
                f"Found {summary['flagged_count']:,} files to clean up "
                f"({summary['flagged_bytes'] / (1024**3):.2f} GB)."
            ),
        )

    def _phase_scan(
        self, job_id: str, sources: list[str], include_other_files: bool = False
    ) -> None:
        self._reset_phase_metrics(0)
        self.db.update_job(
            job_id,
            status="scanning",
            phase="scanning",
            status_message="Scanning folders & drives (no cloud files downloaded yet)...",
        )

        batch: list[tuple[Any, ...]] = []
        found = self.db.count_files(job_id)
        last_ui_update = time.time()

        for priority, root in enumerate(sources):
            self._check_control([root])
            root_abs = os.path.abspath(root)

            for dirpath, dirnames, filenames in os.walk(root_abs, topdown=True):
                self._check_control([root_abs])
                # Skip system/recycle/quarantine directories
                dirnames[:] = [
                    d
                    for d in dirnames
                    if d.lower() not in SKIP_DIR_NAMES and not d.startswith("_PhotoSweep_")
                ]

                for fname in filenames:
                    ext = os.path.splitext(fname)[1].lower()
                    if ext in PHOTO_EXTENSIONS:
                        media_type = "photo"
                    elif ext in VIDEO_EXTENSIONS:
                        media_type = "video"
                    elif include_other_files:
                        fl = fname.lower()
                        if (
                            fl in SKIP_OTHER_FILENAMES
                            or fl.startswith("~$")
                            or ext in SKIP_OTHER_EXTENSIONS
                        ):
                            continue
                        media_type = "other"
                    else:
                        continue

                    full_path = os.path.join(dirpath, fname)
                    try:
                        st = os.stat(full_path)
                    except OSError:
                        if not is_path_accessible(root_abs):
                            raise DriveDisconnectedError(root_abs)
                        continue

                    cloud_only = 1 if is_cloud_only_file(full_path) else 0
                    batch.append(
                        (
                            job_id,
                            full_path,
                            root_abs,
                            priority,
                            fname,
                            normalize_filename(fname),
                            ext,
                            media_type,
                            int(st.st_size),
                            float(st.st_mtime),
                            float(st.st_ctime),
                            cloud_only,
                        )
                    )
                    found += 1

                    if len(batch) >= 250:
                        self.db.insert_files_batch(batch)
                        batch.clear()

                    now = time.time()
                    if now - last_ui_update >= 0.4:
                        self._update_phase_metrics(found, 0)
                        self.db.update_job(
                            job_id,
                            total_files=found,
                            processed_files=found,
                            current_file=full_path,
                        )
                        last_ui_update = now

        if batch:
            self.db.insert_files_batch(batch)

        total = self.db.count_files(job_id)
        self.db.update_job(
            job_id,
            total_files=total,
            processed_files=total,
            current_file="",
            status_message=f"Scan complete: indexed {total:,} photos & videos.",
        )

    def _phase_exact_duplicates(
        self,
        job_id: str,
        sources: list[str],
        onedrive_mode: str,
        min_ssd_free_gb: float,
        enable_media_dupes: bool = True,
        enable_other_dupes: bool = False,
    ) -> None:
        size_groups = self.db.get_size_collision_groups(job_id)
        total_groups = len(size_groups)
        self._reset_phase_metrics(total_groups)
        tracker = ThroughputTracker()
        rem_work = self.db.get_dedup_candidate_work(job_id)
        self._update_phase_metrics(0, total_groups, tracker=tracker, rem_work=rem_work)
        self.db.update_job(
            job_id,
            status="deduping",
            phase="deduping",
            total_files=total_groups,
            processed_files=0,
            status_message=f"Checking exact duplicates across {total_groups:,} size-matched groups...",
        )

        dup_group_idx = 0
        last_ui = time.time()

        for g_idx, size_bytes in enumerate(size_groups, start=1):
            self._check_control(sources)
            files = self.db.get_files_by_size(job_id, size_bytes)
            if len(files) < 2:
                continue

            # Filter candidates according to enabled modes & pre-filter 'other' files by same ext + similar name
            # (avoids downloading cloud-only non-media files that don't share extension/name!)
            eligible: list[dict[str, Any]] = []
            media_files = [
                f for f in files if f["media_type"] in ("photo", "video") and enable_media_dupes
            ]
            if len(media_files) >= 2:
                eligible.extend(media_files)

            if enable_other_dupes:
                other_files = [f for f in files if f["media_type"] == "other"]
                if len(other_files) >= 2:
                    for i, f_a in enumerate(other_files):
                        has_partner = any(
                            i != j
                            and are_names_similar(
                                f_a["filename"], f_b["filename"], f_a["ext"], f_b["ext"]
                            )
                            for j, f_b in enumerate(other_files)
                        )
                        if has_partner:
                            eligible.append(f_a)

            if len(eligible) < 2:
                continue

            # Compute 3-point partial hash for every eligible candidate in this size group
            by_partial: dict[str, list[dict[str, Any]]] = {}
            for f in eligible:
                self._check_control([f["source_root"]])
                if f["decision"] == "exact_duplicate":
                    continue

                path = f["path"]
                was_cloud = bool(f["was_cloud_only"]) or is_cloud_only_file(path)

                if was_cloud and onedrive_mode == "skip_cloud":
                    self.db.update_file(
                        f["id"],
                        decision="skipped_cloud",
                        decision_reason="Skipped OneDrive cloud-only file",
                    )
                    continue

                p_hash = f["partial_hash"]
                if not p_hash:
                    if was_cloud:
                        self._guard_ssd_space(path, min_ssd_free_gb)
                    t0 = time.time()
                    try:
                        p_hash = compute_partial_hash(path)
                        self.db.update_file(f["id"], partial_hash=p_hash)
                        f["partial_hash"] = p_hash
                        # Cloud-only files download full bytes on first open; local files read <= 192KB
                        eff_bytes = int(f["size_bytes"]) if was_cloud else min(int(f["size_bytes"]), 196608)
                        tracker.record(eff_bytes, time.time() - t0, is_cloud=was_cloud)
                    except OSError as exc:
                        if not is_path_accessible(f["source_root"]):
                            raise DriveDisconnectedError(f["source_root"])
                        self.db.update_file(
                            f["id"],
                            decision="error",
                            decision_reason=f"Could not read/download for duplicate check: {exc}",
                        )
                        continue
                    finally:
                        if was_cloud and onedrive_mode == "smart_dehydrate":
                            dehydrate_onedrive_file(path)

                    if was_cloud:
                        rem_work["cloud_cnt"] = max(0, rem_work["cloud_cnt"] - 1)
                        rem_work["cloud_bytes"] = max(0, rem_work["cloud_bytes"] - int(f["size_bytes"]))
                    else:
                        rem_work["local_cnt"] = max(0, rem_work["local_cnt"] - 1)
                        rem_work["local_bytes"] = max(0, rem_work["local_bytes"] - int(f["size_bytes"]))

                by_partial.setdefault(p_hash, []).append(f)

            # Evaluate each partial-hash collision bucket
            for p_hash, bucket in by_partial.items():
                if len(bucket) < 2:
                    continue
                dup_group_idx += 1
                gid = f"dup_{dup_group_idx}"

                # Files are already ordered by: source_priority ASC, shortest filename, oldest mtime
                # Partition bucket into exact-duplicate clusters
                representatives: list[dict[str, Any]] = []
                for cand in bucket:
                    matched_rep: dict[str, Any] | None = None
                    match_method = ""
                    for rep in representatives:
                        cand_is_other = cand["media_type"] == "other"
                        rep_is_other = rep["media_type"] == "other"
                        if cand_is_other != rep_is_other:
                            continue

                        if cand_is_other:
                            # Non-media files: require same byte size + same extension + exact/similar name
                            # + matching 3-point partial hash (already verified by bucket)
                            if are_names_similar(
                                cand["filename"], rep["filename"], cand["ext"], rep["ext"]
                            ):
                                matched_rep = rep
                                match_method = "same size, extension, similar name & partial hash"
                                break
                            continue

                        same_norm_name = cand["normalized_name"] == rep["normalized_name"]
                        if same_norm_name:
                            # Size matches + normalized name matches (e.g. 'IMG_1.jpg' & 'IMG_1 (1).jpg')
                            # + 3-point partial hash matches => confirmed exact duplicate!
                            matched_rep = rep
                            match_method = "same size, name & partial hash"
                            break
                        else:
                            # Names differ -> verify with full SHA-256 hash
                            rep_full = self._ensure_full_hash(
                                rep, onedrive_mode, min_ssd_free_gb
                            )
                            cand_full = self._ensure_full_hash(
                                cand, onedrive_mode, min_ssd_free_gb
                            )
                            if rep_full and cand_full and rep_full == cand_full:
                                matched_rep = rep
                                match_method = "identical full SHA-256 hash"
                                break

                    if matched_rep is not None:
                        self.db.update_file(
                            cand["id"],
                            decision="exact_duplicate",
                            decision_reason=f"Exact duplicate of '{matched_rep['filename']}' ({match_method})",
                            group_id=gid,
                            kept_file_id=matched_rep["id"],
                        )
                    else:
                        representatives.append(cand)
                        self.db.update_file(
                            cand["id"],
                            group_id=gid,
                            kept_file_id=cand["id"],
                        )

            now = time.time()
            if now - last_ui >= 0.35 or g_idx == total_groups:
                self._update_phase_metrics(
                    g_idx, total_groups, tracker=tracker, rem_work=rem_work
                )
                self.db.update_job(
                    job_id,
                    processed_files=g_idx,
                    current_file=files[0]["path"],
                )
                last_ui = now

    def _ensure_full_hash(
        self,
        file_row: dict[str, Any],
        onedrive_mode: str,
        min_ssd_free_gb: float,
    ) -> str | None:
        if file_row.get("full_hash"):
            return str(file_row["full_hash"])
        path = file_row["path"]
        was_cloud = bool(file_row["was_cloud_only"]) or is_cloud_only_file(path)
        if was_cloud:
            self._guard_ssd_space(path, min_ssd_free_gb)
        try:
            fh = compute_full_hash(path)
            self.db.update_file(file_row["id"], full_hash=fh)
            file_row["full_hash"] = fh
            return fh
        except OSError:
            if not is_path_accessible(file_row["source_root"]):
                raise DriveDisconnectedError(file_row["source_root"])
            return None
        finally:
            if was_cloud and onedrive_mode == "smart_dehydrate":
                dehydrate_onedrive_file(path)

    def _phase_analyze_photos(
        self,
        job_id: str,
        sources: list[str],
        preset: Any,
        enable_blur: bool,
        onedrive_mode: str,
        min_ssd_free_gb: float,
    ) -> None:
        done_start, total_photos = self.db.count_photos_for_analysis(job_id)
        self._reset_phase_metrics(total_photos)
        self.db.update_job(
            job_id,
            status="analyzing",
            phase="analyzing",
            total_files=total_photos,
            processed_files=done_start,
            status_message=f"Analyzing {total_photos:,} photos (faces, lighting, sharpness, visual hashes)...",
        )

        if total_photos == 0:
            return

        # Ideal CPU thread count for i5-1035G7 (4C/8T) without starving OS/HDD
        workers = min(6, max(2, (os.cpu_count() or 4) - 1))
        done = done_start
        tracker = ThroughputTracker()
        rem_work = self.db.get_remaining_photo_work(job_id)
        self._update_phase_metrics(done, total_photos, tracker=tracker, rem_work=rem_work)

        def process_one(row: dict[str, Any]) -> tuple[int, dict[str, Any], bool, int]:
            path = row["path"]
            size_b = int(row["size_bytes"])
            was_cloud = bool(row["was_cloud_only"]) or is_cloud_only_file(path)

            if was_cloud and onedrive_mode == "skip_cloud":
                return (
                    int(row["id"]),
                    {
                        "analyzed": 1,
                        "decision": "skipped_cloud",
                        "decision_reason": "Skipped OneDrive cloud-only photo",
                    },
                    was_cloud,
                    0,
                )

            if was_cloud:
                self._guard_ssd_space(path, min_ssd_free_gb)

            try:
                res = analyze_photo(
                    path=path,
                    size_bytes=size_b,
                    preset=preset,
                    enable_blur_detection=enable_blur,
                )
                return int(row["id"]), res, was_cloud, size_b
            except OSError as exc:
                if not is_path_accessible(row["source_root"]):
                    raise DriveDisconnectedError(row["source_root"]) from exc
                return (
                    int(row["id"]),
                    {
                        "analyzed": 1,
                        "decision": "error",
                        "decision_reason": f"Could not download/read file: {exc}",
                    },
                    was_cloud,
                    size_b,
                )
            except Exception as exc:
                return (
                    int(row["id"]),
                    {
                        "analyzed": 1,
                        "decision": "error",
                        "decision_reason": f"Decode error: {exc}",
                    },
                    was_cloud,
                    size_b,
                )
            finally:
                if was_cloud and onedrive_mode == "smart_dehydrate":
                    dehydrate_onedrive_file(path)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            while True:
                self._check_control(sources)
                batch = self.db.get_unanalyzed_photos(job_id, limit=24)
                if not batch:
                    break

                batch_t0 = time.time()
                futures = {pool.submit(process_one, row): row for row in batch}
                updates: list[tuple[dict[str, Any], int]] = []
                last_path = batch[-1]["path"]
                batch_local_bytes = 0
                batch_cloud_bytes = 0

                for fut in as_completed(futures):
                    row = futures[fut]
                    file_id, fields, was_cloud, size_b = fut.result()
                    updates.append((fields, file_id))
                    done += 1
                    last_path = row["path"]
                    if was_cloud:
                        batch_cloud_bytes += size_b
                        rem_work["cloud_cnt"] = max(0, rem_work["cloud_cnt"] - 1)
                        rem_work["cloud_bytes"] = max(0, rem_work["cloud_bytes"] - int(row["size_bytes"]))
                    else:
                        batch_local_bytes += size_b
                        rem_work["local_cnt"] = max(0, rem_work["local_cnt"] - 1)
                        rem_work["local_bytes"] = max(0, rem_work["local_bytes"] - int(row["size_bytes"]))

                self.db.update_files_bulk(updates)

                batch_dur = max(0.001, time.time() - batch_t0)
                if batch_cloud_bytes > 0 and batch_local_bytes == 0:
                    tracker.record(batch_cloud_bytes, batch_dur, is_cloud=True)
                elif batch_local_bytes > 0 and batch_cloud_bytes == 0:
                    tracker.record(batch_local_bytes, batch_dur, is_cloud=False)
                elif batch_cloud_bytes + batch_local_bytes > 0:
                    # Mixed batch: attribute proportionally (cloud dominates wait time)
                    tracker.record(batch_cloud_bytes + batch_local_bytes, batch_dur, is_cloud=True)

                self._update_phase_metrics(done, total_photos, tracker=tracker, rem_work=rem_work)

                self.db.update_job(
                    job_id,
                    processed_files=done,
                    current_file=last_path,
                )

    def _phase_cluster_similar(self, job_id: str, preset: Any) -> None:
        self.db.update_job(
            job_id,
            status="clustering",
            phase="clustering",
            current_file="",
            status_message="Grouping similar photos & selecting the best shot in each burst...",
        )
        photos = self.db.get_photos_for_clustering(job_id)
        self._reset_phase_metrics(len(photos))
        self._check_control()

        updates = cluster_similar_photos(photos, preset)
        if updates:
            # Write in chunks of 500
            for i in range(0, len(updates), 500):
                self._check_control()
                self.db.update_files_bulk(updates[i : i + 500])

        self._update_phase_metrics(len(photos), len(photos))

    # ---------------- Execution Pipeline (Quarantine / Delete / Copy Kept / Undo) ----------------

    def _run_execution_pipeline(self, job_id: str, options: dict[str, Any]) -> None:
        job = self.db.get_job(job_id)
        assert job is not None
        mode = options["mode"]
        categories = options.get("categories") or [
            "exact_duplicate",
            "blurry",
            "similar_loser",
        ]
        batch_id = f"act_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:5]}"
        retry_failed = bool(options.get("retry_failed", False))

        if mode == "quarantine":
            self._exec_quarantine(job, batch_id, categories, retry_failed=retry_failed)
        elif mode == "delete":
            self._exec_delete(job, batch_id, categories, retry_failed=retry_failed)
        elif mode == "copy_kept":
            self._exec_copy_kept(job, batch_id, options)
        elif mode == "undo_quarantine":
            self._exec_undo_quarantine(job)

        summary = self.db.compute_summary(job_id)
        self.db.update_job(
            job_id,
            status="analyzed",
            phase="completed",
            current_file="",
            summary_json=summary,
        )

    def _exec_quarantine(
        self,
        job: dict[str, Any],
        batch_id: str,
        categories: list[str],
        retry_failed: bool = False,
    ) -> None:
        job_id = job["id"]
        files = self.db.get_flagged_files(
            job_id,
            categories=categories,
            only_pending=not retry_failed,
            only_failed=retry_failed,
            include_failed=True,
        )
        total = len(files)
        self._reset_phase_metrics(total, action_mode="quarantine")
        self.db.update_job(
            job_id,
            status="executing",
            phase="quarantining",
            total_files=total,
            processed_files=0,
            status_message=f"Moving {total:,} flagged files to same-drive quarantine folders...",
        )

        moved = 0
        failed = 0
        last_ui = time.time()
        for idx, f in enumerate(files, start=1):
            self._check_control([f["source_root"]])
            src = Path(f["path"])
            if not src.exists():
                self.db.update_file(
                    f["id"],
                    action_status="failed",
                    action_error="File no longer found on disk",
                )
                failed += 1
                continue

            # Place quarantine on the SAME drive for instant zero-space moves!
            drive_root = Path(get_drive_root(src))
            try:
                rel = src.relative_to(f["source_root"])
            except ValueError:
                rel = Path(src.name)

            dst = drive_root / "_PhotoSweep_Quarantine" / job_id / f["decision"] / rel
            try:
                actual_dst = move_file_preserve_all_metadata(src, dst)
                self.db.update_file(
                    f["id"],
                    action_status="quarantined",
                    action_dest_path=str(actual_dst),
                    action_error="",
                )
                self.db.log_action(
                    job_id, batch_id, f["id"], "quarantine", str(src), str(actual_dst)
                )
                moved += 1
            except OSError as exc:
                if not is_path_accessible(f["source_root"]):
                    raise DriveDisconnectedError(f["source_root"])
                self.db.update_file(
                    f["id"],
                    action_status="failed",
                    action_error=f"Move failed: {exc}",
                )
                failed += 1

            now = time.time()
            if total <= 50 or idx == total or (now - last_ui) >= 0.15:
                self._update_phase_metrics(idx, total)
                self.db.update_job(job_id, processed_files=idx, current_file=str(src))
                last_ui = now

        msg = f"Moved {moved:,} files to _PhotoSweep_Quarantine (instant & reversible via Undo)."
        if failed > 0:
            msg += f" {failed:,} file(s) could not be moved."
        self.db.update_job(job_id, status_message=msg)

    def _exec_delete(
        self,
        job: dict[str, Any],
        batch_id: str,
        categories: list[str],
        retry_failed: bool = False,
    ) -> None:
        job_id = job["id"]
        files = self.db.get_flagged_files(
            job_id,
            categories=categories,
            only_pending=not retry_failed,
            only_failed=retry_failed,
            include_failed=True,
        )
        total = len(files)
        self._reset_phase_metrics(total, action_mode="delete")
        self.db.update_job(
            job_id,
            status="executing",
            phase="deleting",
            total_files=total,
            processed_files=0,
            status_message=(
                f"Retrying deletion of {total:,} failed files..."
                if retry_failed
                else f"Permanently deleting {total:,} flagged files..."
            ),
        )

        deleted = 0
        failed = 0
        last_ui = time.time()
        for idx, f in enumerate(files, start=1):
            self._check_control([f["source_root"]])
            src = Path(f["path"])
            try:
                delete_file_with_retry(src)
                self.db.update_file(
                    f["id"], action_status="deleted", action_error=""
                )
                self.db.log_action(job_id, batch_id, f["id"], "delete", str(src), None)
                deleted += 1
            except OSError as exc:
                if not is_path_accessible(f["source_root"]):
                    raise DriveDisconnectedError(f["source_root"])
                self.db.update_file(
                    f["id"],
                    action_status="failed",
                    action_error=f"Delete failed: {exc}",
                )
                failed += 1

            now = time.time()
            if total <= 50 or idx == total or (now - last_ui) >= 0.15:
                self._update_phase_metrics(idx, total)
                self.db.update_job(job_id, processed_files=idx, current_file=str(src))
                last_ui = now

        if total == 0:
            msg = "No pending flagged files left to delete."
        elif failed == 0:
            msg = f"All {deleted:,} selected flagged files have been permanently deleted."
        else:
            msg = f"Deleted {deleted:,} files. {failed:,} file(s) could not be deleted."
        self.db.update_job(job_id, status_message=msg)

    def _exec_copy_kept(
        self, job: dict[str, Any], batch_id: str, options: dict[str, Any]
    ) -> None:
        job_id = job["id"]
        dest_root = Path(os.path.abspath(str(options["destination"]).strip()))
        dest_root.mkdir(parents=True, exist_ok=True)
        folder_layout = options.get("folder_layout", "date_ym")  # 'date_ym' | 'preserve'
        min_free_gb = float(job["config"].get("min_ssd_free_gb", 15.0))
        dest_is_onedrive = is_onedrive_path(dest_root)
        onedrive_mode = job["config"].get("onedrive_mode", "smart_dehydrate")

        files = self.db.get_kept_files(job_id)
        total = len(files)
        self._reset_phase_metrics(total, action_mode="copy_kept")
        self.db.update_job(
            job_id,
            status="executing",
            phase="copying",
            total_files=total,
            processed_files=0,
            status_message=f"Copying {total:,} kept files to {dest_root} (preserving EXIF & timestamps)...",
        )

        copied = 0
        recent_od_copies: list[Path] = []

        for idx, f in enumerate(files, start=1):
            self._check_control([f["source_root"], get_drive_root(dest_root)])
            if f["action_status"] == "copied" and f.get("action_dest_path") and os.path.exists(f["action_dest_path"]):
                copied += 1
                continue

            src = Path(f["path"])
            if not src.exists():
                continue

            # OneDrive destination SSD guard: pause & dehydrate synced copies if disk runs low
            if dest_is_onedrive:
                self._throttle_onedrive_destination(
                    job_id, dest_root, recent_od_copies, min_free_gb
                )
            else:
                free_gb = get_drive_free_gb(dest_root)
                needed_gb = (int(f["size_bytes"]) / (1024**3)) + 1.0
                if free_gb < needed_gb:
                    raise LowDiskSpaceError(get_drive_root(dest_root), free_gb, needed_gb)

            if folder_layout == "preserve":
                try:
                    rel = src.relative_to(f["source_root"])
                except ValueError:
                    rel = Path(src.name)
                dst = dest_root / rel
            else:
                # Clean YYYY/MM organization based on EXIF DateTimeOriginal (fallback to mtime)
                ts = f["exif_timestamp"] if f["exif_timestamp"] is not None else f["mtime"]
                dt = datetime.fromtimestamp(float(ts))
                dst = dest_root / f"{dt.year:04d}" / f"{dt.month:02d}" / src.name

            src_was_cloud = bool(f["was_cloud_only"]) or is_cloud_only_file(src)
            try:
                actual_dst = copy_file_preserve_all_metadata(src, dst)
                self.db.update_file(
                    f["id"],
                    action_status="copied",
                    action_dest_path=str(actual_dst),
                    action_error="",
                )
                self.db.log_action(
                    job_id, batch_id, f["id"], "copy_kept", str(src), str(actual_dst)
                )
                copied += 1
                if dest_is_onedrive:
                    recent_od_copies.append(actual_dst)
                    if len(recent_od_copies) >= 25:
                        for p in recent_od_copies:
                            dehydrate_onedrive_file(p)
                        recent_od_copies.clear()
            except OSError as exc:
                if not is_path_accessible(f["source_root"]):
                    raise DriveDisconnectedError(f["source_root"])
                if not is_path_accessible(get_drive_root(dest_root)):
                    raise DriveDisconnectedError(str(dest_root))
                self.db.update_file(
                    f["id"], action_status="failed", action_error=f"Copy failed: {exc}"
                )
            finally:
                if src_was_cloud and onedrive_mode == "smart_dehydrate":
                    dehydrate_onedrive_file(src)

            if idx % 5 == 0 or idx == total:
                self._update_phase_metrics(idx, total)
                self.db.update_job(job_id, processed_files=idx, current_file=str(src))

        self.db.update_job(
            job_id,
            status_message=f"Copied {copied:,} kept files to {dest_root} with original EXIF & timestamps intact.",
        )

    def _throttle_onedrive_destination(
        self,
        job_id: str,
        dest_root: Path,
        recent_copies: list[Path],
        min_free_gb: float,
    ) -> None:
        while get_drive_free_gb(dest_root) < min_free_gb:
            self._check_control([get_drive_root(dest_root)])
            for p in recent_copies:
                dehydrate_onedrive_file(p)
            free_gb = get_drive_free_gb(dest_root)
            self.db.update_job(
                job_id,
                status_message=(
                    f"OneDrive SSD guard: {free_gb:.1f} GB free (< {min_free_gb:.0f} GB). "
                    "Waiting for OneDrive to finish uploading & freeing space..."
                ),
            )
            time.sleep(5.0)

    def _exec_undo_quarantine(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        actions = self.db.get_undoable_quarantine_actions(job_id)
        total = len(actions)
        self._reset_phase_metrics(total, action_mode="undo_quarantine")
        self.db.update_job(
            job_id,
            status="executing",
            phase="restoring",
            total_files=total,
            processed_files=0,
            status_message=f"Restoring {total:,} quarantined files to original paths...",
        )

        restored = 0
        for idx, act in enumerate(actions, start=1):
            self._check_control()
            dst_path = act["dst_path"]
            src_path = act["src_path"]
            if dst_path and os.path.exists(dst_path):
                move_file_preserve_all_metadata(dst_path, src_path)
                self.db.mark_action_undone(act["id"], act["file_id"])
                restored += 1
            if idx % 10 == 0 or idx == total:
                self._update_phase_metrics(idx, total)
                self.db.update_job(job_id, processed_files=idx, current_file=src_path)

        self.db.update_job(
            job_id,
            status_message=f"Undo complete: restored {restored:,} files to their original locations.",
        )

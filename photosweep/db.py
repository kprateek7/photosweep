"""SQLite persistence layer with WAL checkpointing for resumable jobs and undo logs."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import DB_PATH


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    status TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'idle',
    config_json TEXT NOT NULL,
    total_files INTEGER NOT NULL DEFAULT 0,
    processed_files INTEGER NOT NULL DEFAULT 0,
    scan_completed INTEGER NOT NULL DEFAULT 0,
    dedup_completed INTEGER NOT NULL DEFAULT 0,
    analysis_completed INTEGER NOT NULL DEFAULT 0,
    cluster_completed INTEGER NOT NULL DEFAULT 0,
    current_file TEXT DEFAULT '',
    disconnected_path TEXT DEFAULT '',
    status_message TEXT DEFAULT '',
    error_message TEXT DEFAULT '',
    summary_json TEXT DEFAULT '{}',
    analysis_duration_sec REAL NOT NULL DEFAULT 0.0,
    delete_duration_sec REAL NOT NULL DEFAULT 0.0,
    last_action_duration_sec REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    path TEXT NOT NULL,
    source_root TEXT NOT NULL,
    source_priority INTEGER NOT NULL DEFAULT 0,
    filename TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    ext TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime REAL NOT NULL,
    ctime REAL NOT NULL,
    was_cloud_only INTEGER NOT NULL DEFAULT 0,

    partial_hash TEXT,
    full_hash TEXT,
    analyzed INTEGER NOT NULL DEFAULT 0,

    width INTEGER DEFAULT 0,
    height INTEGER DEFAULT 0,
    exif_timestamp REAL,
    iso INTEGER,
    exposure_time REAL,

    dhash TEXT,
    phash TEXT,
    brightness REAL,
    face_count INTEGER DEFAULT 0,
    face_sharpness REAL,
    face_tenengrad REAL,
    whole_sharpness REAL,
    eyes_open_ratio REAL DEFAULT 0.0,
    smile_score REAL DEFAULT 0.0,
    quality_score REAL DEFAULT 0.0,
    is_dark INTEGER DEFAULT 0,
    is_motion_risk INTEGER DEFAULT 0,

    decision TEXT NOT NULL DEFAULT 'keep',
    decision_reason TEXT DEFAULT '',
    group_id TEXT,
    kept_file_id INTEGER,

    action_status TEXT NOT NULL DEFAULT 'pending',
    action_dest_path TEXT,
    action_error TEXT DEFAULT '',

    UNIQUE(job_id, path)
);

CREATE INDEX IF NOT EXISTS idx_files_job_size ON files(job_id, size_bytes);
CREATE INDEX IF NOT EXISTS idx_files_job_decision ON files(job_id, decision);
CREATE INDEX IF NOT EXISTS idx_files_job_analyzed ON files(job_id, media_type, decision, analyzed);
CREATE INDEX IF NOT EXISTS idx_files_job_group ON files(job_id, group_id);

CREATE TABLE IF NOT EXISTS actions_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    file_id INTEGER NOT NULL,
    action_type TEXT NOT NULL,
    src_path TEXT NOT NULL,
    dst_path TEXT,
    created_at REAL NOT NULL,
    undone INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_actions_job_batch ON actions_log(job_id, batch_id, undone);
"""


class Database:
    """Thread-safe SQLite wrapper using WAL mode for crash-safe resume."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA cache_size=-32000;")
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(SCHEMA_SQL)
                # Safe migrations for existing databases
                file_cols = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(files)").fetchall()
                }
                if "action_error" not in file_cols:
                    conn.execute("ALTER TABLE files ADD COLUMN action_error TEXT DEFAULT ''")

                job_cols = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
                }
                if "analysis_duration_sec" not in job_cols:
                    conn.execute(
                        "ALTER TABLE jobs ADD COLUMN analysis_duration_sec REAL NOT NULL DEFAULT 0.0"
                    )
                if "delete_duration_sec" not in job_cols:
                    conn.execute(
                        "ALTER TABLE jobs ADD COLUMN delete_duration_sec REAL NOT NULL DEFAULT 0.0"
                    )
                if "last_action_duration_sec" not in job_cols:
                    conn.execute(
                        "ALTER TABLE jobs ADD COLUMN last_action_duration_sec REAL NOT NULL DEFAULT 0.0"
                    )
                conn.commit()
            finally:
                conn.close()

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._connect()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # ---------------- Jobs ----------------

    def create_job(self, job_id: str, config: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO jobs (id, created_at, updated_at, status, phase, config_json)
                VALUES (?, ?, ?, 'created', 'idle', ?)
                """,
                (job_id, now, now, json.dumps(config)),
            )
        return self.get_job(job_id)  # type: ignore[return-value]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.session() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row_to_job(row) if row else None

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.session() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._row_to_job(r) for r in rows]

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        cols = []
        vals = []
        for k, v in fields.items():
            if k in ("config_json", "summary_json") and not isinstance(v, str):
                v = json.dumps(v)
            cols.append(f"{k} = ?")
            vals.append(v)
        vals.append(job_id)
        with self.session() as conn:
            conn.execute(f"UPDATE jobs SET {', '.join(cols)} WHERE id = ?", vals)

    def add_job_duration(self, job_id: str, field: str, delta_sec: float) -> None:
        """Accumulate active elapsed duration (in seconds) on a job timer column."""
        if field not in ("analysis_duration_sec", "delete_duration_sec"):
            raise ValueError(f"Invalid duration field: {field}")
        delta = max(0.0, float(delta_sec))
        with self.session() as conn:
            conn.execute(
                f"UPDATE jobs SET {field} = COALESCE({field}, 0.0) + ?, updated_at = ? WHERE id = ?",
                (delta, time.time(), job_id),
            )

    def _row_to_job(self, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["config"] = json.loads(d.pop("config_json") or "{}")
        d["summary"] = json.loads(d.pop("summary_json") or "{}")
        return d

    # ---------------- Files ----------------

    def insert_files_batch(self, rows: list[tuple[Any, ...]]) -> None:
        if not rows:
            return
        with self.session() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO files (
                    job_id, path, source_root, source_priority,
                    filename, normalized_name, ext, media_type,
                    size_bytes, mtime, ctime, was_cloud_only
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def count_files(self, job_id: str) -> int:
        with self.session() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM files WHERE job_id = ?", (job_id,)
            ).fetchone()
            return int(row["c"]) if row else 0

    def get_size_collision_groups(self, job_id: str) -> list[int]:
        """Return distinct file sizes (> 0 bytes) shared by 2+ files in this job."""
        with self.session() as conn:
            rows = conn.execute(
                """
                SELECT size_bytes FROM files
                WHERE job_id = ? AND size_bytes > 0
                GROUP BY size_bytes
                HAVING COUNT(*) > 1
                """,
                (job_id,),
            ).fetchall()
            return [int(r["size_bytes"]) for r in rows]

    def get_files_by_size(self, job_id: str, size_bytes: int) -> list[dict[str, Any]]:
        with self.session() as conn:
            rows = conn.execute(
                """
                SELECT * FROM files
                WHERE job_id = ? AND size_bytes = ?
                ORDER BY source_priority ASC, LENGTH(filename) ASC, mtime ASC, id ASC
                """,
                (job_id, size_bytes),
            ).fetchall()
            return [dict(r) for r in rows]

    def update_file(self, file_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = [f"{k} = ?" for k in fields]
        vals = list(fields.values()) + [file_id]
        with self.session() as conn:
            conn.execute(f"UPDATE files SET {', '.join(cols)} WHERE id = ?", vals)

    def update_files_bulk(self, updates: list[tuple[dict[str, Any], int]]) -> None:
        if not updates:
            return
        with self.session() as conn:
            for fields, file_id in updates:
                cols = [f"{k} = ?" for k in fields]
                vals = list(fields.values()) + [file_id]
                conn.execute(f"UPDATE files SET {', '.join(cols)} WHERE id = ?", vals)

    def get_unanalyzed_photos(self, job_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self.session() as conn:
            rows = conn.execute(
                """
                SELECT * FROM files
                WHERE job_id = ? AND media_type = 'photo' AND decision = 'keep' AND analyzed = 0
                ORDER BY source_priority ASC, id ASC
                LIMIT ?
                """,
                (job_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def count_photos_for_analysis(self, job_id: str) -> tuple[int, int]:
        """Return (done_count, total_count) of non-duplicate photos."""
        with self.session() as conn:
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN analyzed = 1 THEN 1 ELSE 0 END) AS done,
                    COUNT(*) AS total
                FROM files
                WHERE job_id = ? AND media_type = 'photo' AND decision != 'exact_duplicate'
                """,
                (job_id,),
            ).fetchone()
            if not row:
                return 0, 0
            return int(row["done"] or 0), int(row["total"] or 0)

    def get_remaining_photo_work(self, job_id: str) -> dict[str, int]:
        """Return remaining local & cloud counts and byte totals for Phase 3 photo analysis."""
        with self.session() as conn:
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN was_cloud_only = 0 THEN 1 ELSE 0 END) AS local_cnt,
                    COALESCE(SUM(CASE WHEN was_cloud_only = 0 THEN size_bytes ELSE 0 END), 0) AS local_bytes,
                    SUM(CASE WHEN was_cloud_only = 1 THEN 1 ELSE 0 END) AS cloud_cnt,
                    COALESCE(SUM(CASE WHEN was_cloud_only = 1 THEN size_bytes ELSE 0 END), 0) AS cloud_bytes
                FROM files
                WHERE job_id = ? AND media_type = 'photo' AND decision = 'keep' AND analyzed = 0
                """,
                (job_id,),
            ).fetchone()
            if not row:
                return {"local_cnt": 0, "local_bytes": 0, "cloud_cnt": 0, "cloud_bytes": 0}
            return {
                "local_cnt": int(row["local_cnt"] or 0),
                "local_bytes": int(row["local_bytes"] or 0),
                "cloud_cnt": int(row["cloud_cnt"] or 0),
                "cloud_bytes": int(row["cloud_bytes"] or 0),
            }

    def get_dedup_candidate_work(self, job_id: str) -> dict[str, int]:
        """Return local & cloud counts and approximate read bytes for unhashed size-collision files."""
        with self.session() as conn:
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN was_cloud_only = 0 THEN 1 ELSE 0 END) AS local_cnt,
                    COALESCE(SUM(CASE WHEN was_cloud_only = 0 THEN size_bytes ELSE 0 END), 0) AS local_bytes,
                    SUM(CASE WHEN was_cloud_only = 1 THEN 1 ELSE 0 END) AS cloud_cnt,
                    COALESCE(SUM(CASE WHEN was_cloud_only = 1 THEN size_bytes ELSE 0 END), 0) AS cloud_bytes
                FROM files
                WHERE job_id = ?
                  AND partial_hash IS NULL
                  AND decision = 'keep'
                  AND size_bytes IN (
                      SELECT size_bytes FROM files
                      WHERE job_id = ? AND size_bytes > 0
                      GROUP BY size_bytes HAVING COUNT(*) > 1
                  )
                """,
                (job_id, job_id),
            ).fetchone()
            if not row:
                return {"local_cnt": 0, "local_bytes": 0, "cloud_cnt": 0, "cloud_bytes": 0}
            return {
                "local_cnt": int(row["local_cnt"] or 0),
                "local_bytes": int(row["local_bytes"] or 0),
                "cloud_cnt": int(row["cloud_cnt"] or 0),
                "cloud_bytes": int(row["cloud_bytes"] or 0),
            }

    def get_photos_for_clustering(self, job_id: str) -> list[dict[str, Any]]:
        """Fetch all kept, analyzed photos that have perceptual hashes."""
        with self.session() as conn:
            rows = conn.execute(
                """
                SELECT id, path, filename, source_priority, size_bytes, mtime,
                       exif_timestamp, dhash, phash, width, height,
                       face_count, face_sharpness, whole_sharpness,
                       eyes_open_ratio, smile_score, brightness, quality_score
                FROM files
                WHERE job_id = ?
                  AND media_type = 'photo'
                  AND decision = 'keep'
                  AND analyzed = 1
                  AND phash IS NOT NULL
                  AND dhash IS NOT NULL
                ORDER BY COALESCE(exif_timestamp, mtime) ASC, id ASC
                """,
                (job_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_file_by_id(self, file_id: int) -> dict[str, Any] | None:
        with self.session() as conn:
            row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
            return dict(row) if row else None

    def get_flagged_files(
        self,
        job_id: str,
        categories: list[str] | None = None,
        only_pending: bool = True,
        only_failed: bool = False,
        include_failed: bool = False,
    ) -> list[dict[str, Any]]:
        cats = categories or ["exact_duplicate", "blurry", "similar_loser"]
        placeholders = ",".join("?" for _ in cats)
        sql = f"SELECT * FROM files WHERE job_id = ? AND decision IN ({placeholders})"
        params: list[Any] = [job_id, *cats]
        if only_failed:
            sql += " AND action_status = 'failed'"
        elif only_pending:
            if include_failed:
                sql += " AND action_status IN ('pending', 'restored', 'failed')"
            else:
                sql += " AND action_status IN ('pending', 'restored')"
        sql += " ORDER BY source_priority ASC, id ASC"
        with self.session() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def get_failed_files(self, job_id: str) -> list[dict[str, Any]]:
        """Return all files that failed deletion/action OR failed download/read during analysis."""
        with self.session() as conn:
            rows = conn.execute(
                """
                SELECT id, path, filename, media_type, size_bytes, decision,
                       decision_reason, action_status, action_error
                FROM files
                WHERE job_id = ?
                  AND (action_status = 'failed' OR decision = 'error')
                ORDER BY action_status DESC, id ASC
                """,
                (job_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def reset_error_files_for_retry(self, job_id: str) -> int:
        """
        Reset only files that failed to download/read during analysis (decision='error')
        so resuming the analysis pipeline retries just those files without re-scanning everything.
        """
        with self.session() as conn:
            cur = conn.execute(
                """
                UPDATE files
                SET decision = 'keep',
                    decision_reason = '',
                    analyzed = 0,
                    partial_hash = NULL,
                    full_hash = NULL
                WHERE job_id = ? AND decision = 'error'
                """,
                (job_id,),
            )
            count = int(cur.rowcount or 0)
            if count > 0:
                conn.execute(
                    """
                    UPDATE jobs
                    SET dedup_completed = 0,
                        analysis_completed = 0,
                        cluster_completed = 0,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (time.time(), job_id),
                )
            return count

    def get_kept_files(self, job_id: str) -> list[dict[str, Any]]:
        with self.session() as conn:
            rows = conn.execute(
                """
                SELECT * FROM files
                WHERE job_id = ? AND decision = 'keep'
                ORDER BY source_priority ASC, COALESCE(exif_timestamp, mtime) ASC, id ASC
                """,
                (job_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_sample_flagged(
        self, job_id: str, category: str, limit: int = 40, offset: int = 0
    ) -> list[dict[str, Any]]:
        with self.session() as conn:
            rows = conn.execute(
                """
                SELECT f.*, k.path AS kept_path, k.filename AS kept_filename,
                       k.quality_score AS kept_quality_score
                FROM files f
                LEFT JOIN files k ON f.kept_file_id = k.id
                WHERE f.job_id = ? AND f.decision = ?
                ORDER BY f.size_bytes DESC, f.id ASC
                LIMIT ? OFFSET ?
                """,
                (job_id, category, limit, offset),
            ).fetchall()
            return [dict(r) for r in rows]

    def compute_summary(self, job_id: str) -> dict[str, Any]:
        with self.session() as conn:
            rows = conn.execute(
                """
                SELECT decision, media_type,
                       COUNT(*) AS cnt,
                       COALESCE(SUM(size_bytes), 0) AS bytes,
                       SUM(CASE WHEN action_status = 'quarantined' THEN 1 ELSE 0 END) AS quarantined_cnt,
                       SUM(CASE WHEN action_status = 'deleted' THEN 1 ELSE 0 END) AS deleted_cnt,
                       COALESCE(SUM(CASE WHEN action_status = 'deleted' THEN size_bytes ELSE 0 END), 0) AS deleted_bytes,
                       SUM(CASE WHEN action_status = 'copied' THEN 1 ELSE 0 END) AS copied_cnt,
                       SUM(CASE WHEN action_status = 'failed' THEN 1 ELSE 0 END) AS failed_cnt,
                       SUM(CASE WHEN action_status IN ('pending', 'restored') THEN 1 ELSE 0 END) AS pending_cnt
                FROM files
                WHERE job_id = ?
                GROUP BY decision, media_type
                """,
                (job_id,),
            ).fetchall()

            undo_row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM actions_log
                WHERE job_id = ? AND action_type = 'quarantine' AND undone = 0
                """,
                (job_id,),
            ).fetchone()

        categories: dict[str, dict[str, int]] = {
            "exact_duplicate": {"count": 0, "photos": 0, "videos": 0, "other": 0, "bytes": 0},
            "blurry": {"count": 0, "photos": 0, "videos": 0, "other": 0, "bytes": 0},
            "similar_loser": {"count": 0, "photos": 0, "videos": 0, "other": 0, "bytes": 0},
            "keep": {"count": 0, "photos": 0, "videos": 0, "other": 0, "bytes": 0},
            "skipped_cloud": {"count": 0, "photos": 0, "videos": 0, "other": 0, "bytes": 0},
            "error": {"count": 0, "photos": 0, "videos": 0, "other": 0, "bytes": 0},
        }
        flagged_decisions = {"exact_duplicate", "blurry", "similar_loser"}
        total_files = 0
        total_bytes = 0
        quarantined = 0
        deleted = 0
        deleted_bytes = 0
        copied = 0
        failed_actions = 0
        failed_flagged = 0
        pending_flagged = 0

        for r in rows:
            dec = r["decision"]
            mtype = r["media_type"]
            cnt = int(r["cnt"])
            b = int(r["bytes"])
            total_files += cnt
            total_bytes += b
            quarantined += int(r["quarantined_cnt"] or 0)
            deleted += int(r["deleted_cnt"] or 0)
            deleted_bytes += int(r["deleted_bytes"] or 0)
            copied += int(r["copied_cnt"] or 0)
            f_cnt = int(r["failed_cnt"] or 0)
            failed_actions += f_cnt
            if dec in flagged_decisions:
                pending_flagged += int(r["pending_cnt"] or 0)
                failed_flagged += f_cnt
            if dec in categories:
                categories[dec]["count"] += cnt
                categories[dec]["bytes"] += b
                if mtype == "photo":
                    categories[dec]["photos"] += cnt
                elif mtype == "video":
                    categories[dec]["videos"] += cnt
                else:
                    categories[dec]["other"] += cnt

        flagged_count = (
            categories["exact_duplicate"]["count"]
            + categories["blurry"]["count"]
            + categories["similar_loser"]["count"]
        )
        flagged_bytes = (
            categories["exact_duplicate"]["bytes"]
            + categories["blurry"]["bytes"]
            + categories["similar_loser"]["bytes"]
        )
        download_errors = categories["error"]["count"]
        all_flagged_deleted = (
            flagged_count > 0
            and pending_flagged == 0
            and failed_flagged == 0
            and deleted > 0
            and quarantined == 0
        )

        return {
            "total_files": total_files,
            "total_bytes": total_bytes,
            "flagged_count": flagged_count,
            "flagged_bytes": flagged_bytes,
            "kept_count": categories["keep"]["count"],
            "kept_bytes": categories["keep"]["bytes"],
            "categories": categories,
            "actioned": {
                "quarantined": quarantined,
                "deleted": deleted,
                "deleted_bytes": deleted_bytes,
                "copied": copied,
                "failed": failed_actions,
                "failed_flagged": failed_flagged,
                "pending_flagged": pending_flagged,
                "download_errors": download_errors,
            },
            "all_flagged_deleted": all_flagged_deleted,
            "has_failures": (failed_actions > 0 or download_errors > 0),
            "can_undo_quarantine": int(undo_row["c"] or 0) > 0 if undo_row else False,
        }

    # ---------------- Actions / Undo Log ----------------

    def log_action(
        self,
        job_id: str,
        batch_id: str,
        file_id: int,
        action_type: str,
        src_path: str,
        dst_path: str | None = None,
    ) -> None:
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO actions_log (job_id, batch_id, file_id, action_type, src_path, dst_path, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, batch_id, file_id, action_type, src_path, dst_path, time.time()),
            )

    def get_undoable_quarantine_actions(self, job_id: str) -> list[dict[str, Any]]:
        with self.session() as conn:
            rows = conn.execute(
                """
                SELECT * FROM actions_log
                WHERE job_id = ? AND action_type = 'quarantine' AND undone = 0
                ORDER BY id DESC
                """,
                (job_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_action_undone(self, action_id: int, file_id: int) -> None:
        with self.session() as conn:
            conn.execute("UPDATE actions_log SET undone = 1 WHERE id = ?", (action_id,))
            conn.execute(
                "UPDATE files SET action_status = 'restored', action_dest_path = NULL WHERE id = ?",
                (file_id,),
            )

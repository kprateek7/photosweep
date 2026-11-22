"""FastAPI web server for PhotoSweep local UI."""

from __future__ import annotations

import csv
import io
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps
from pydantic import BaseModel

from .config import APP_DATA_DIR, PRESETS
from .db import Database
from .engine import DriveDisconnectedError, JobEngine
from .win_utils import (
    get_available_drives,
    get_drive_free_gb,
    get_quick_shortcuts,
    is_onedrive_path,
    is_path_accessible,
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class StartAnalysisRequest(BaseModel):
    sources: list[str]
    preset: str = "balanced"
    onedrive_mode: str = "smart_dehydrate"
    min_ssd_free_gb: float = 15.0
    enable_duplicates: bool = True
    enable_other_duplicates: bool = False
    enable_blur: bool = True
    enable_similar: bool = True


class ExecuteActionRequest(BaseModel):
    mode: str  # quarantine | delete | copy_kept | undo_quarantine
    categories: list[str] | None = None
    destination: str | None = None
    folder_layout: str = "date_ym"  # date_ym | preserve
    retry_failed: bool = False


def create_app(db_path: Path | str | None = None) -> FastAPI:
    app = FastAPI(title="PhotoSweep", version="1.0.0")
    db = Database(db_path=db_path)
    engine = JobEngine(db)

    app.state.db = db
    app.state.engine = engine

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(STATIC_DIR / "index.html"))

    @app.get("/api/system")
    def get_system_info() -> dict[str, Any]:
        return {
            "drives": get_available_drives(),
            "shortcuts": get_quick_shortcuts(),
            "presets": {
                k: {
                    "name": v.name,
                    "label": v.label,
                    "description": v.description,
                }
                for k, v in PRESETS.items()
            },
            "app_data_dir": str(APP_DATA_DIR),
            "c_drive_free_gb": round(get_drive_free_gb("C:\\"), 1),
        }

    @app.get("/api/browse")
    def browse_directory(path: str = Query(default="")) -> dict[str, Any]:
        """Browse local drives & subfolders cleanly from the web UI."""
        if not path:
            return {
                "current": "",
                "parent": "",
                "entries": [
                    {
                        "name": d["label"],
                        "path": d["path"],
                        "is_drive": True,
                        "free_gb": d["free_gb"],
                        "total_gb": d["total_gb"],
                        "is_onedrive": False,
                    }
                    for d in get_available_drives()
                ],
            }

        target = os.path.abspath(path)
        if not is_path_accessible(target):
            raise HTTPException(status_code=404, detail=f"Path not accessible: {target}")

        parent = os.path.dirname(target.rstrip("\\/"))
        if parent == target.rstrip("\\/"):
            parent = ""  # Back to drives list

        entries: list[dict[str, Any]] = []
        try:
            with os.scandir(target) as it:
                for entry in it:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    if entry.name.startswith("$") or entry.name.lower() in (
                        "system volume information",
                    ):
                        continue
                    entries.append(
                        {
                            "name": entry.name,
                            "path": entry.path,
                            "is_drive": False,
                            "is_onedrive": is_onedrive_path(entry.path),
                        }
                    )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=f"Permission denied: {exc}")

        entries.sort(key=lambda e: e["name"].lower())
        return {
            "current": target,
            "parent": parent,
            "is_onedrive": is_onedrive_path(target),
            "entries": entries,
        }

    @app.get("/api/jobs")
    def list_jobs() -> dict[str, Any]:
        return {
            "active_job_id": engine.get_active_job_id(),
            "jobs": db.list_jobs(limit=15),
        }

    @app.post("/api/jobs/analyze")
    def start_analysis(req: StartAnalysisRequest) -> dict[str, Any]:
        try:
            job = engine.start_analysis(req.model_dump())
            return {"job": job}
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/api/jobs/{job_id}")
    def get_job_status(job_id: str) -> dict[str, Any]:
        job = db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        is_active = engine.get_active_job_id() == job_id and engine.is_busy()
        runtime = engine.get_runtime_stats() if is_active else {}

        # If drive was disconnected, actively check whether it's back online!
        reconnected = False
        if job["status"] == "drive_disconnected" and job.get("disconnected_path"):
            reconnected = is_path_accessible(job["disconnected_path"])

        return {
            "job": job,
            "is_active": is_active,
            "runtime": runtime,
            "drive_reconnected": reconnected,
            "c_drive_free_gb": round(get_drive_free_gb("C:\\"), 1),
        }

    @app.post("/api/jobs/{job_id}/pause")
    def pause_job(job_id: str) -> dict[str, Any]:
        if engine.get_active_job_id() == job_id:
            engine.pause()
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, Any]:
        if engine.get_active_job_id() == job_id:
            engine.cancel()
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/resume")
    def resume_job(job_id: str) -> dict[str, Any]:
        try:
            job = engine.resume_analysis(job_id)
            return {"job": job}
        except DriveDisconnectedError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/jobs/{job_id}/retry-analysis")
    def retry_analysis(job_id: str) -> dict[str, Any]:
        try:
            job = engine.retry_failed_analysis(job_id)
            return {"job": job}
        except DriveDisconnectedError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/jobs/{job_id}/execute")
    def execute_action(job_id: str, req: ExecuteActionRequest) -> dict[str, Any]:
        try:
            job = engine.execute_action(job_id, req.model_dump())
            return {"job": job}
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/api/jobs/{job_id}/samples")
    def get_samples(
        job_id: str,
        category: str = Query(default="exact_duplicate"),
        limit: int = Query(default=30, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        if category not in ("exact_duplicate", "blurry", "similar_loser"):
            raise HTTPException(status_code=400, detail="Invalid category")
        items = db.get_sample_flagged(job_id, category=category, limit=limit, offset=offset)
        return {"items": items}

    @app.get("/api/jobs/{job_id}/failed-csv")
    def download_failed_csv(job_id: str) -> Response:
        """Download CSV of all files that failed deletion/action or failed download/read."""
        job = db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        failed_rows = db.get_failed_files(job_id)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            ["Stage", "Category", "Filename", "Path", "Size (Bytes)", "Error Reason"]
        )
        for r in failed_rows:
            stage = (
                "Delete/Action Failed"
                if r["action_status"] == "failed"
                else "Download/Read Failed"
            )
            reason = r["action_error"] or r["decision_reason"] or "Unknown error"
            writer.writerow(
                [stage, r["decision"], r["filename"], r["path"], r["size_bytes"], reason]
            )
        headers = {
            "Content-Disposition": f'attachment; filename="photosweep_failed_{job_id}.csv"'
        }
        return Response(
            content=buf.getvalue(), media_type="text/csv; charset=utf-8", headers=headers
        )

    @app.get("/api/thumb/{file_id}")
    def get_thumbnail(file_id: int) -> Response:
        """Serve a fast 260px JPEG thumbnail for optional spot-checking."""
        f = db.get_file_by_id(file_id)
        if not f or f["media_type"] != "photo":
            raise HTTPException(status_code=404, detail="Not an image")
        p = f["path"]
        if not os.path.exists(p):
            raise HTTPException(status_code=404, detail="File not found on disk")
        try:
            with Image.open(p) as raw:
                img = ImageOps.exif_transpose(raw)
                img.thumbnail((260, 260))
                if img.mode != "RGB":
                    img = img.convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=78)
                return Response(content=buf.getvalue(), media_type="image/jpeg")
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    return app

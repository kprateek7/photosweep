"""OS utilities (Windows & macOS): OneDrive/iCloud detection, exact timestamps, drive discovery, keep-awake."""

from __future__ import annotations

import ctypes
import os
import shutil
import string
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"

if IS_WINDOWS:
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

_caffeinate_proc: subprocess.Popen[Any] | None = None

# Win32 file attributes for OneDrive Files On-Demand
FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_PINNED = 0x00080000
FILE_ATTRIBUTE_UNPINNED = 0x00100000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF

CLOUD_ONLY_MASK = (
    FILE_ATTRIBUTE_OFFLINE
    | FILE_ATTRIBUTE_RECALL_ON_OPEN
    | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)

# Win32 CreateFile constants for SetFileTime
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_WRITE_ATTRIBUTES = 0x0100
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# Win32 SetThreadExecutionState flags
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

DRIVE_TYPES = {
    0: "Unknown",
    1: "No Root",
    2: "Removable",
    3: "Local Disk",
    4: "Network",
    5: "CD-ROM",
    6: "RAM Disk",
}


def get_onedrive_roots() -> list[str]:
    """Return normalized local paths to any configured OneDrive folders (Windows & macOS)."""
    roots: list[str] = []
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        val = os.environ.get(var)
        if val and os.path.isdir(val):
            norm = os.path.normcase(os.path.abspath(val))
            if norm not in roots:
                roots.append(norm)
    if IS_MACOS:
        cloud_storage = Path.home() / "Library" / "CloudStorage"
        if cloud_storage.is_dir():
            for child in cloud_storage.iterdir():
                if child.is_dir() and child.name.lower().startswith("onedrive"):
                    norm = os.path.normcase(os.path.abspath(str(child)))
                    if norm not in roots:
                        roots.append(norm)
    return roots


def is_onedrive_path(path: str | Path) -> bool:
    """Return True if path resides inside a OneDrive sync folder."""
    norm = os.path.normcase(os.path.abspath(str(path)))
    for root in get_onedrive_roots():
        if norm == root or norm.startswith(root + os.sep):
            return True
    return False


def get_file_attributes(path: str | Path) -> int:
    if not IS_WINDOWS:
        return 0
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return 0 if attrs == INVALID_FILE_ATTRIBUTES else int(attrs)
    except Exception:
        return 0


def is_cloud_only_file(path: str | Path) -> bool:
    """Return True if a Windows OneDrive file is currently cloud-only (dehydrated placeholder)."""
    if not IS_WINDOWS:
        return False
    attrs = get_file_attributes(path)
    return bool(attrs & CLOUD_ONLY_MASK)


def dehydrate_onedrive_file(path: str | Path) -> bool:
    """Ask Windows OneDrive Files On-Demand to free local disk space (attrib +U -P)."""
    if not IS_WINDOWS:
        return False
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        res = subprocess.run(
            ["attrib", "+U", "-P", str(path)],
            capture_output=True,
            timeout=10,
            creationflags=creationflags,
        )
        return res.returncode == 0
    except Exception:
        return False


def get_drive_root(path: str | Path) -> str:
    """Return drive/volume root ('C:\\' on Windows, '/Volumes/<Name>' on macOS external drives, or '/')."""
    p = os.path.abspath(str(path))
    if IS_WINDOWS:
        drive, _ = os.path.splitdrive(p)
        return (drive + os.sep) if drive else os.sep
    if IS_MACOS and p.startswith("/Volumes/"):
        parts = Path(p).parts
        if len(parts) >= 3:
            return os.path.join(parts[0], parts[1], parts[2])
    return os.sep


def get_system_drive_info() -> tuple[str, float]:
    """Return (label, free_gb) for the primary OS drive (C: on Windows, Macintosh HD on macOS)."""
    if IS_WINDOWS:
        return "C:", round(get_drive_free_gb("C:\\"), 1)
    if IS_MACOS:
        return "Mac SSD", round(get_drive_free_gb("/"), 1)
    return "Disk /", round(get_drive_free_gb("/"), 1)


def get_drive_free_gb(path: str | Path) -> float:
    """Return free disk space in GB for the volume containing path."""
    try:
        usage = shutil.disk_usage(get_drive_root(path))
        return usage.free / (1024**3)
    except Exception:
        return 0.0


def is_path_accessible(path: str | Path) -> bool:
    """Check whether a drive or directory is currently mounted and accessible."""
    try:
        p = str(path)
        return os.path.exists(p) and os.path.isdir(p)
    except OSError:
        return False


def get_available_drives() -> list[dict[str, Any]]:
    """List mounted Windows drives and convenient user folders (OneDrive, Pictures)."""
    drives: list[dict[str, Any]] = []
    if IS_WINDOWS:
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for idx, letter in enumerate(string.ascii_uppercase):
            if bitmask & (1 << idx):
                root = f"{letter}:\\"
                try:
                    dtype = ctypes.windll.kernel32.GetDriveTypeW(root)
                    if dtype in (5, 0, 1):  # Skip CD-ROM / invalid
                        continue
                    vol_buf = ctypes.create_unicode_buffer(261)
                    ctypes.windll.kernel32.GetVolumeInformationW(
                        root, vol_buf, 261, None, None, None, None, 0
                    )
                    label = vol_buf.value or DRIVE_TYPES.get(dtype, "Drive")
                    usage = shutil.disk_usage(root)
                    drives.append(
                        {
                            "path": root,
                            "label": f"{letter}: ({label})",
                            "type": DRIVE_TYPES.get(dtype, "Drive"),
                            "total_gb": round(usage.total / (1024**3), 1),
                            "free_gb": round(usage.free / (1024**3), 1),
                            "is_onedrive": False,
                        }
                    )
                except OSError:
                    continue
    elif IS_MACOS:
        try:
            usage = shutil.disk_usage("/")
            drives.append(
                {
                    "path": "/",
                    "label": "Macintosh HD (/)",
                    "type": "Local Disk",
                    "total_gb": round(usage.total / (1024**3), 1),
                    "free_gb": round(usage.free / (1024**3), 1),
                    "is_onedrive": False,
                }
            )
        except OSError:
            pass
        volumes = Path("/Volumes")
        if volumes.is_dir():
            for vol in sorted(volumes.iterdir(), key=lambda x: x.name.lower()):
                try:
                    if not vol.is_dir() or vol.is_symlink():
                        continue
                    usage = shutil.disk_usage(str(vol))
                    drives.append(
                        {
                            "path": str(vol),
                            "label": vol.name,
                            "type": "External Volume",
                            "total_gb": round(usage.total / (1024**3), 1),
                            "free_gb": round(usage.free / (1024**3), 1),
                            "is_onedrive": False,
                        }
                    )
                except OSError:
                    continue
    else:
        usage = shutil.disk_usage("/")
        drives.append(
            {
                "path": "/",
                "label": "Root (/)",
                "type": "Local Disk",
                "total_gb": round(usage.total / (1024**3), 1),
                "free_gb": round(usage.free / (1024**3), 1),
                "is_onedrive": False,
            }
        )

    return drives


def get_quick_shortcuts() -> list[dict[str, Any]]:
    """Return quick-access shortcuts like OneDrive, iCloud Drive, Pictures, Desktop."""
    shortcuts: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(label: str, path: str | None, is_od: bool = False) -> None:
        if not path or not os.path.isdir(path):
            return
        norm = os.path.normcase(os.path.abspath(path))
        if norm in seen:
            return
        seen.add(norm)
        shortcuts.append(
            {
                "label": label,
                "path": os.path.abspath(path),
                "is_onedrive": is_od or is_onedrive_path(path),
            }
        )

    for od_root in get_onedrive_roots():
        label = Path(od_root).name if IS_MACOS else "OneDrive"
        add(label, od_root, True)
        pics = os.path.join(od_root, "Pictures")
        if os.path.isdir(pics):
            add(f"{label} Pictures", pics, True)

    home = Path.home()
    if IS_MACOS:
        icloud = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
        if icloud.is_dir():
            add("iCloud Drive", str(icloud), True)

    add("Pictures", str(home / "Pictures"))
    add("Desktop", str(home / "Desktop"))
    return shortcuts


def prevent_sleep(enable: bool) -> None:
    """Prevent or re-allow system sleep during long-running jobs (Windows & macOS)."""
    global _caffeinate_proc
    if IS_WINDOWS:
        try:
            flags = (ES_CONTINUOUS | ES_SYSTEM_REQUIRED) if enable else ES_CONTINUOUS
            ctypes.windll.kernel32.SetThreadExecutionState(flags)
        except Exception:
            pass
    elif IS_MACOS:
        try:
            if enable:
                if _caffeinate_proc is None or _caffeinate_proc.poll() is not None:
                    _caffeinate_proc = subprocess.Popen(
                        ["caffeinate", "-i", "-s"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
            else:
                if _caffeinate_proc is not None and _caffeinate_proc.poll() is None:
                    _caffeinate_proc.terminate()
                _caffeinate_proc = None
        except Exception:
            pass


def copy_windows_file_times(src: str | Path, dst: str | Path) -> None:
    """Copy exact NTFS Creation, LastAccess, and LastWrite timestamps via Win32 API."""
    if not IS_WINDOWS:
        return
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.GetFileTime.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
    ]
    kernel32.SetFileTime.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    h_src = kernel32.CreateFileW(
        str(src),
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if h_src == INVALID_HANDLE_VALUE:
        return

    c_time, a_time, w_time = FILETIME(), FILETIME(), FILETIME()
    try:
        ok = kernel32.GetFileTime(
            h_src, ctypes.byref(c_time), ctypes.byref(a_time), ctypes.byref(w_time)
        )
    finally:
        kernel32.CloseHandle(h_src)

    if not ok:
        return

    h_dst = kernel32.CreateFileW(
        str(dst),
        FILE_WRITE_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if h_dst == INVALID_HANDLE_VALUE:
        return

    try:
        kernel32.SetFileTime(
            h_dst, ctypes.byref(c_time), ctypes.byref(a_time), ctypes.byref(w_time)
        )
    finally:
        kernel32.CloseHandle(h_dst)


def ensure_unique_dest_path(dst: Path) -> Path:
    """Avoid overwriting an existing file by appending _1, _2, etc. if needed."""
    if not dst.exists():
        return dst
    stem = dst.stem
    suffix = dst.suffix
    parent = dst.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def copy_file_preserve_all_metadata(
    src: str | Path,
    dst: str | Path,
    retries: int = 4,
) -> Path:
    """
    Copy raw file bytes untouched (100% EXIF preserved) plus exact Windows
    Created, Accessed, and Modified timestamps. Retries on transient OneDrive locks.
    """
    src_p = Path(src)
    dst_p = ensure_unique_dest_path(Path(dst))
    dst_p.parent.mkdir(parents=True, exist_ok=True)

    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            shutil.copy2(str(src_p), str(dst_p))
            copy_windows_file_times(src_p, dst_p)
            return dst_p
        except (PermissionError, OSError) as exc:
            last_err = exc
            if not is_path_accessible(get_drive_root(src_p)):
                raise
            time.sleep(0.5 * (2**attempt))

    raise last_err if last_err else OSError(f"Failed to copy {src_p} to {dst_p}")


def move_file_preserve_all_metadata(
    src: str | Path,
    dst: str | Path,
    retries: int = 4,
) -> Path:
    """
    Move file preserving EXIF and exact Windows Created/Modified timestamps.
    Same-drive moves are atomic/instant; cross-drive moves copy + verify size + remove.
    """
    src_p = Path(src)
    dst_p = ensure_unique_dest_path(Path(dst))
    dst_p.parent.mkdir(parents=True, exist_ok=True)

    same_drive = os.path.normcase(get_drive_root(src_p)) == os.path.normcase(
        get_drive_root(dst_p)
    )

    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            if same_drive:
                os.replace(str(src_p), str(dst_p))
                return dst_p
            else:
                expected_size = src_p.stat().st_size
                actual_dst = copy_file_preserve_all_metadata(src_p, dst_p, retries=2)
                if actual_dst.stat().st_size != expected_size:
                    actual_dst.unlink(missing_ok=True)
                    raise IOError("Size mismatch after cross-drive copy")
                src_p.unlink()
                return actual_dst
        except (PermissionError, OSError) as exc:
            last_err = exc
            if not is_path_accessible(get_drive_root(src_p)):
                raise
            time.sleep(0.5 * (2**attempt))

    raise last_err if last_err else OSError(f"Failed to move {src_p} to {dst_p}")


def delete_file_with_retry(path: str | Path, retries: int = 4) -> None:
    """Delete a file with retry/backoff for OneDrive sync locks."""
    p = Path(path)
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            if p.exists():
                p.unlink()
            return
        except (PermissionError, OSError) as exc:
            last_err = exc
            if not is_path_accessible(get_drive_root(p)):
                raise
            time.sleep(0.5 * (2**attempt))
    raise last_err if last_err else OSError(f"Failed to delete {p}")

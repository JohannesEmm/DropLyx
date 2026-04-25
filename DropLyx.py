import os
import sys
import time
import json
import re
import threading
import shutil
import hashlib
import logging
import logging.handlers
import html as html_module
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from datetime import datetime
from difflib import unified_diff, Differ
from PIL import Image, ImageDraw, ImageFont
import pystray
import psutil

try:
    from plyer import notification as plyer_notif
    HAS_PLYER = True
except:
    HAS_PLYER = False

CONFIG_FILE = Path.home() / ".lyx_sync_config.json"
LOG_FILE = Path.home() / ".droplyx.log"
PID_FILE = Path.home() / ".droplyx.pid"
LOCK_SUFFIX = ".lock"
BASELINE_SUFFIX = ".baseline"
POLL_INTERVAL = 1                # seconds between monitor loop iterations
LOCK_HEARTBEAT_INTERVAL = 30     # seconds between heartbeat writes into our own lock files
LOCK_STALE_THRESHOLD = 120       # seconds before a lock without a fresh heartbeat is marked stale
SYNC_DEBOUNCE_SECS = 2.0         # seconds a changed hash must be stable before we react (avoids mid-Dropbox-sync reactions)

state = {
    "watch_dirs": [],
    "locked_files": {},
    "my_locks": set(),
    "file_baselines": {},          # {filepath: baseline_path}
    "file_hashes": {},             # {filepath: last_known_hash}
    "file_mtimes": {},             # {filepath: last_modification_time}
    "file_change_pending": {},     # {filepath: (new_hash, first_seen_time)} debounce tracker
    "pending_merges": {},          # {filepath: remote_backup_path}
    "processed_conflicts": set(),  # Dropbox conflict files already handled
    "merge_on_save": False,
    "start_on_login": False,
    "running": True,
    "icon": None,
    "menu_needs_update": False,
    "window_cache": [],
    "window_cache_time": 0,
    "window_cache_ttl": 5,
    "log_server_port": None,
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_logger = None


def setup_logging():
    global _logger
    _logger = logging.getLogger("droplyx")
    _logger.setLevel(logging.INFO)
    if not _logger.handlers:
        handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        _logger.addHandler(handler)
    _logger.info("DropLyx started (PID %d)", os.getpid())


def log(message, level="INFO"):
    if _logger is None:
        return
    getattr(_logger, level.lower(), _logger.info)(message)


# ---------------------------------------------------------------------------
# Single-instance enforcement (PID file)
# ---------------------------------------------------------------------------

def check_single_instance():
    """Return True if it is safe to proceed (no other instance running)."""
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            if psutil.pid_exists(old_pid):
                return False
        except Exception:
            pass
    try:
        PID_FILE.write_text(str(os.getpid()))
    except Exception:
        pass
    return True


def cleanup_pid_file():
    try:
        if PID_FILE.exists():
            PID_FILE.unlink()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def notify(title, message):
    log(f"[NOTIFY] {title}: {message}")
    if HAS_PLYER:
        try:
            plyer_notif.notify(title=title, message=message, timeout=4)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Resources / icon
# ---------------------------------------------------------------------------

def get_resource_path(relative_path):
    """Resolve a bundled resource path (dev or PyInstaller frozen)."""
    try:
        base_path = Path(sys._MEIPASS)
    except Exception:
        base_path = Path(__file__).parent
    return base_path / relative_path


def create_icon(color="lightblue"):
    size = 64
    colors = {
        "lightblue": (30, 60, 140, 255),
        "green": (46, 204, 113, 255),
        "red": (231, 76, 60, 255),
        "orange": (243, 156, 18, 255),
    }
    status_color = colors.get(color, colors["lightblue"])

    try:
        logo_path = get_resource_path("DropLyx_logo.png")
        img = Image.open(logo_path)
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        img = img.resize((size, size), Image.Resampling.LANCZOS)
        img = img.copy()
        draw = ImageDraw.Draw(img)
        indicator_size = 20
        indicator_x = size - indicator_size - 2
        indicator_y = size - indicator_size - 2
        draw.ellipse(
            [indicator_x - 2, indicator_y - 2,
             indicator_x + indicator_size + 2, indicator_y + indicator_size + 2],
            fill=(255, 255, 255, 255),
            outline=None,
        )
        draw.ellipse(
            [indicator_x, indicator_y,
             indicator_x + indicator_size, indicator_y + indicator_size],
            fill=status_color,
            outline=None,
        )
        return img
    except Exception:
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([2, 2, size - 2, size - 2], fill=status_color,
                     outline=(255, 255, 255), width=2)
        try:
            font = ImageFont.truetype("arial.ttf", 28)
        except Exception:
            font = ImageFont.load_default()
        draw.text((20, 18), "D", fill=(255, 255, 255), font=font)
        return img


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def save_config():
    config = {
        "watch_dirs": state["watch_dirs"],
        "merge_on_save": state.get("merge_on_save", False),
        "start_on_login": state.get("start_on_login", False),
    }
    try:
        CONFIG_FILE.write_text(json.dumps(config, indent=2))
    except Exception as e:
        log(f"Failed to save config: {e}", "ERROR")


def load_config():
    """Returns (watch_dirs, merge_on_save, start_on_login). Handles legacy format & bad JSON."""
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text())
            # Handle legacy single-dir format
            if "watch_dir" in data and "watch_dirs" not in data:
                watch_dirs = [data["watch_dir"]]
            else:
                watch_dirs = data.get("watch_dirs", [])
            merge_on_save = data.get("merge_on_save", False)
            start_on_login = data.get("start_on_login", False)
            return watch_dirs, merge_on_save, start_on_login
        except Exception as e:
            log(f"Failed to load config (using defaults): {e}", "WARNING")
    return [], False, False


# ---------------------------------------------------------------------------
# Lock file helpers  — new JSON format with heartbeat + backward compat
# ---------------------------------------------------------------------------

def write_lock_data(lock_path):
    """Write a JSON lock file containing user, pid, and current heartbeat time."""
    data = {
        "user": get_username(),
        "pid": os.getpid(),
        "heartbeat": time.time(),
    }
    lock_path.write_text(json.dumps(data))


def read_lock_data(lock_path):
    """Read a lock file.  Returns (user, heartbeat_or_None, pid_or_None).
    Gracefully handles both the new JSON format and the old plain-text format."""
    try:
        content = lock_path.read_text().strip()
        data = json.loads(content)
        return data.get("user", "unknown"), data.get("heartbeat"), data.get("pid")
    except (json.JSONDecodeError, ValueError):
        # Old format: plain username string
        return content, None, None
    except Exception:
        return "unknown", None, None


def is_stale_lock(lock_path):
    """Return True if the lock's heartbeat (or mtime for old format) is too old."""
    try:
        _, heartbeat, _ = read_lock_data(lock_path)
        if heartbeat is not None:
            return (time.time() - heartbeat) > LOCK_STALE_THRESHOLD
        # Old format — fall back to file mtime
        return (time.time() - lock_path.stat().st_mtime) > LOCK_STALE_THRESHOLD
    except Exception:
        return False


def cleanup_stale_own_locks():
    """On startup, remove lock files we left behind from a previous crashed instance."""
    username = get_username()
    current_pid = os.getpid()
    for d in state.get("watch_dirs", []):
        try:
            for lock_file in Path(d).rglob(f"*{LOCK_SUFFIX}"):
                user, heartbeat, pid = read_lock_data(lock_file)
                if user.rstrip(" (stale?)") != username:
                    continue
                stale = False
                if pid is not None and pid != current_pid:
                    stale = not psutil.pid_exists(int(pid))
                elif heartbeat is not None:
                    stale = (time.time() - heartbeat) > LOCK_STALE_THRESHOLD
                else:
                    stale = (time.time() - lock_file.stat().st_mtime) > LOCK_STALE_THRESHOLD
                if stale:
                    try:
                        lock_file.unlink()
                        log(f"Cleaned up stale lock: {lock_file}")
                    except Exception as e:
                        log(f"Failed to remove stale lock {lock_file}: {e}", "WARNING")
        except Exception as e:
            log(f"Error scanning for stale locks in {d}: {e}", "WARNING")


def get_username():
    return os.getenv("USER") or os.getenv("USERNAME") or "unknown"


# ---------------------------------------------------------------------------
# File detection
# ---------------------------------------------------------------------------

def get_lyx_open_files():
    open_files = []

    if sys.platform == "win32":
        # Method 1: Fast process-based detection
        for proc in psutil.process_iter(["name", "cmdline"]):
            try:
                proc_name = proc.info.get("name", "")
                if proc_name and "lyx" in proc_name.lower():
                    cmdline = proc.info.get("cmdline") or []
                    for arg in cmdline:
                        if arg and isinstance(arg, str) and arg.endswith(".lyx"):
                            filepath = Path(arg)
                            if filepath.exists():
                                for watch_dir in state.get("watch_dirs", []):
                                    try:
                                        filepath.resolve().relative_to(Path(watch_dir).resolve())
                                        open_files.append(str(filepath.resolve()))
                                        break
                                    except ValueError:
                                        continue

                    # Method 2: Open file handles (may need elevation)
                    try:
                        for f in proc.open_files():
                            if hasattr(f, "path") and f.path.endswith(".lyx"):
                                filepath = Path(f.path)
                                for watch_dir in state.get("watch_dirs", []):
                                    try:
                                        filepath.resolve().relative_to(Path(watch_dir).resolve())
                                        open_files.append(f.path)
                                        break
                                    except ValueError:
                                        continue
                    except (psutil.AccessDenied, AttributeError):
                        pass
            except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
                pass

        # Method 3: Window title fallback (slow, cached)
        if not open_files:
            try:
                import pygetwindow as gw
                current_time = time.time()
                if (current_time - state["window_cache_time"]) < state["window_cache_ttl"]:
                    windows = state["window_cache"]
                else:
                    windows = gw.getAllWindows()
                    state["window_cache"] = windows
                    state["window_cache_time"] = current_time

                for window in windows:
                    title = window.title
                    if title and "LyX" in title and ".lyx" in title:
                        parts = title.split(" - LyX")[0]
                        if "(" in parts and ")" in parts:
                            filename = parts.split("(")[0].strip()
                            folder_path = parts[parts.find("(") + 1:parts.find(")")]
                            if folder_path.startswith("~\\"):
                                relative_folder = folder_path[2:]
                                relative_path = relative_folder + "\\" + filename
                                possible_bases = [
                                    Path.home(),
                                    Path.home().parent,
                                    Path("C:\\"),
                                ]
                                for watch_dir in state.get("watch_dirs", []):
                                    possible_bases.append(Path(watch_dir).parent)
                                filepath = None
                                for base in possible_bases:
                                    test_path = base / relative_path
                                    if test_path.exists() and test_path.suffix == ".lyx":
                                        filepath = test_path
                                        break
                                if filepath:
                                    open_files.append(str(filepath.resolve()))
                            else:
                                filepath = Path(folder_path) / filename
                                if filepath.exists() and filepath.suffix == ".lyx":
                                    open_files.append(str(filepath.resolve()))
                        else:
                            filename = parts.strip()
                            if filename.endswith(".lyx"):
                                for watch_dir in state.get("watch_dirs", []):
                                    for lyx_file in Path(watch_dir).rglob(filename):
                                        if lyx_file.is_file():
                                            open_files.append(str(lyx_file.resolve()))
                                            break
            except Exception:
                pass
    else:
        # Linux / macOS
        for proc in psutil.process_iter(["name", "open_files"]):
            try:
                if proc.info["name"] == "lyx":
                    for f in proc.info.get("open_files") or []:
                        if f.path.endswith(".lyx"):
                            open_files.append(f.path)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    return list(set(open_files))


# ---------------------------------------------------------------------------
# Hashing / baseline helpers
# ---------------------------------------------------------------------------

def compute_file_hash(filepath):
    try:
        with open(filepath, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return None


def create_baseline(filepath):
    baseline_path = Path(f"{filepath}{BASELINE_SUFFIX}")
    try:
        shutil.copy2(filepath, baseline_path)
        state["file_baselines"][filepath] = str(baseline_path)
        state["file_hashes"][filepath] = compute_file_hash(filepath)
        return True
    except Exception:
        return False


def remove_baseline(filepath):
    baseline_path = Path(f"{filepath}{BASELINE_SUFFIX}")
    if baseline_path.exists():
        try:
            baseline_path.unlink()
        except Exception:
            pass
    state["file_baselines"].pop(filepath, None)
    state["file_hashes"].pop(filepath, None)


# ---------------------------------------------------------------------------
# Dropbox conflict detection & three-way merge
# ---------------------------------------------------------------------------

def is_dropbox_conflict_file(filepath):
    filename = Path(filepath).name
    conflict_keywords = [
        "conflicted copy", "copia in conflitto", "conflitto",
        "konflikt", "copia en conflicto", "conflicto",
        "copie en conflit", "conflit", "conflict",
    ]
    keywords_pattern = "|".join(re.escape(kw) for kw in conflict_keywords)
    pattern = rf"\(.*({keywords_pattern}).*\d{{4}}-\d{{2}}-\d{{2}}.*\)\.lyx$"
    return bool(re.search(pattern, filename, re.IGNORECASE))


def get_original_file_from_conflict(conflict_filepath):
    filepath = Path(conflict_filepath)
    pattern = r"\s*\([^)]*\d{4}-\d{2}-\d{2}[^)]*\)"
    original_name = re.sub(pattern, "", filepath.name, flags=re.IGNORECASE)
    original_path = filepath.parent / original_name
    return str(original_path) if original_path.exists() else None


def detect_conflicts(baseline_lines, local_lines, remote_lines):
    conflicts = []
    for i in range(max(len(baseline_lines), len(local_lines), len(remote_lines))):
        bl = baseline_lines[i] if i < len(baseline_lines) else ""
        ll = local_lines[i] if i < len(local_lines) else ""
        rl = remote_lines[i] if i < len(remote_lines) else ""
        if bl != ll and bl != rl and ll != rl:
            conflicts.append(i)
    return len(conflicts) > 0, conflicts


def perform_three_way_merge(baseline_lines, local_lines, remote_lines):
    merged_lines = []
    max_len = max(len(baseline_lines), len(local_lines), len(remote_lines))
    for i in range(max_len):
        bl = baseline_lines[i] if i < len(baseline_lines) else None
        ll = local_lines[i] if i < len(local_lines) else None
        rl = remote_lines[i] if i < len(remote_lines) else None
        if ll == rl:
            if ll is not None:
                merged_lines.append(ll)
        elif ll == bl:
            if rl is not None:
                merged_lines.append(rl)
        elif rl == bl:
            if ll is not None:
                merged_lines.append(ll)
        else:
            if ll is not None:
                merged_lines.append(ll)
            elif rl is not None:
                merged_lines.append(rl)
    return merged_lines


def handle_dropbox_conflict(conflict_filepath):
    original_filepath = get_original_file_from_conflict(conflict_filepath)
    if not original_filepath:
        return False

    baseline_path = Path(f"{original_filepath}{BASELINE_SUFFIX}")
    if not baseline_path.exists():
        notify("Dropbox Conflict Detected",
               f"{Path(conflict_filepath).name}\nPlease manually resolve the conflict.")
        return False

    try:
        with open(baseline_path, "r", encoding="utf-8", errors="ignore") as f:
            baseline_lines = f.readlines()
        with open(original_filepath, "r", encoding="utf-8", errors="ignore") as f:
            local_lines = f.readlines()
        with open(conflict_filepath, "r", encoding="utf-8", errors="ignore") as f:
            remote_lines = f.readlines()

        has_conflicts, conflict_line_nums = detect_conflicts(baseline_lines, local_lines, remote_lines)
        merged_lines = perform_three_way_merge(baseline_lines, local_lines, remote_lines)

        if has_conflicts:
            shutil.copy2(original_filepath, Path(f"{original_filepath}.local_backup"))
            shutil.copy2(conflict_filepath, Path(f"{original_filepath}.remote_backup"))
            shutil.copy2(original_filepath, Path(f"{original_filepath}.pre_merge_backup"))
            with open(original_filepath, "w", encoding="utf-8") as f:
                f.writelines(merged_lines)
            notify("Dropbox Conflict - Manual Resolution Needed",
                   f"{Path(original_filepath).name}\n"
                   f"Conflicts at lines: {', '.join(map(str, conflict_line_nums[:5]))}\n"
                   f"Backup files created.")
            try:
                Path(conflict_filepath).unlink()
            except Exception:
                pass
            return False
        else:
            with open(original_filepath, "w", encoding="utf-8") as f:
                f.writelines(merged_lines)
            try:
                Path(conflict_filepath).unlink()
                notify("Dropbox Conflict Auto-Merged",
                       f"{Path(original_filepath).name}\nChanges merged successfully.")
                return True
            except Exception as e:
                notify("Dropbox Conflict - Merge Warning",
                       f"{Path(original_filepath).name}\nMerged but couldn't remove conflict file: {e}")
                return False

    except Exception as e:
        notify("Dropbox Conflict - Merge Error",
               f"{Path(conflict_filepath).name}\nError during merge: {e}")
        return False


def merge_files(filepath, local_version_path=None):
    baseline_path = Path(f"{filepath}{BASELINE_SUFFIX}")
    if not baseline_path.exists():
        return ("error", "No baseline found")
    try:
        with open(baseline_path, "r", encoding="utf-8", errors="replace") as f:
            baseline_lines = f.readlines()
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            remote_lines = f.readlines()
        if local_version_path and Path(local_version_path).exists():
            with open(local_version_path, "r", encoding="utf-8", errors="replace") as f:
                local_lines = f.readlines()
        else:
            local_lines = baseline_lines

        if remote_lines == baseline_lines:
            return ("success", "No remote changes detected")
        if local_lines == baseline_lines:
            return ("success", "No local changes - accepting remote version")

        has_conflicts, conflict_lines = detect_conflicts(baseline_lines, local_lines, remote_lines)
        if has_conflicts:
            backup_remote = Path(f"{filepath}.remote_backup")
            backup_local = Path(f"{filepath}.local_backup")
            shutil.copy2(filepath, backup_remote)
            if local_version_path:
                shutil.copy2(local_version_path, backup_local)
            return ("conflict",
                    f"Conflicts at {len(conflict_lines)} line(s).\n"
                    f"Backups: {backup_remote.name}, {backup_local.name}")

        merged_lines = perform_three_way_merge(baseline_lines, local_lines, remote_lines)
        backup_path = Path(f"{filepath}.pre_merge_backup")
        shutil.copy2(filepath, backup_path)
        with open(filepath, "w", encoding="utf-8") as f:
            f.writelines(merged_lines)
        return ("success", f"Successfully merged changes.\nBackup: {backup_path.name}")

    except Exception as e:
        return ("error", f"Merge error: {e}")


def perform_merge_on_save(filepath):
    baseline_path = Path(f"{filepath}{BASELINE_SUFFIX}")
    if not baseline_path.exists():
        return False
    try:
        with open(baseline_path, "r", encoding="utf-8", errors="replace") as f:
            baseline_lines = f.readlines()
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            local_lines = f.readlines()
        if local_lines == baseline_lines:
            return False

        if filepath in state["pending_merges"]:
            remote_backup_path = state["pending_merges"][filepath]
            if Path(remote_backup_path).exists():
                with open(remote_backup_path, "r", encoding="utf-8", errors="replace") as f:
                    remote_lines = f.readlines()
                has_conflicts, conflict_lines = detect_conflicts(baseline_lines, local_lines, remote_lines)
                merged_lines = perform_three_way_merge(baseline_lines, local_lines, remote_lines)

                if has_conflicts:
                    shutil.copy2(filepath, Path(f"{filepath}.local_backup"))
                    shutil.copy2(remote_backup_path, Path(f"{filepath}.remote_backup"))
                    shutil.copy2(filepath, Path(f"{filepath}.pre_merge_backup"))
                    with open(filepath, "w", encoding="utf-8") as f:
                        f.writelines(merged_lines)
                    start_lyx_reload_watcher(filepath)
                    notify("Merge on Save - Conflicts Detected",
                           f"{Path(filepath).name}\nConflicts at {len(conflict_lines)} line(s).\n"
                           "Please resolve manually after reload.")
                else:
                    with open(filepath, "w", encoding="utf-8") as f:
                        f.writelines(merged_lines)
                    start_lyx_reload_watcher(filepath)
                    notify("Merge on Save - Success",
                           f"{Path(filepath).name}\nRemote changes merged and LyX reloaded.")
                    create_baseline(filepath)

                try:
                    Path(remote_backup_path).unlink()
                except Exception:
                    pass
                state["pending_merges"].pop(filepath, None)
                return True

        return False
    except Exception as e:
        notify("Merge on Save - Error", f"{Path(filepath).name}\nError: {e}")
        return False


# ---------------------------------------------------------------------------
# LyX reload-dialog auto-dismissal
# ---------------------------------------------------------------------------

def _auto_reload_lyx_worker(filepath, window_s=6.0):
    """Background thread: watch for LyX's 'file changed on disk' dialog for
    `window_s` seconds after a merge-on-save write and auto-click the first
    button (Yes / Reload).  Uses only built-in ctypes — no extra deps."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        import ctypes.wintypes as wt

        user32 = ctypes.windll.user32
        BM_CLICK = 0x00F5
        # ctypes callback type for EnumWindows / EnumChildWindows
        WinProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

        def _text(hwnd):
            n = user32.GetWindowTextLengthW(hwnd) + 1
            buf = ctypes.create_unicode_buffer(n)
            user32.GetWindowTextW(hwnd, buf, n)
            return buf.value

        def _cls(hwnd):
            buf = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, buf, 64)
            return buf.value

        def _rect_wh(hwnd):
            r = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            return r.right - r.left, r.bottom - r.top

        deadline = time.time() + window_s
        while time.time() < deadline:
            time.sleep(0.4)
            top_hwnds = []
            cb_top = WinProc(lambda h, _: top_hwnds.append(h) or True)
            user32.EnumWindows(cb_top, 0)

            clicked = False
            for hwnd in top_hwnds:
                if not user32.IsWindowVisible(hwnd):
                    continue
                if "LyX" not in _text(hwnd):
                    continue
                w, h = _rect_wh(hwnd)
                if w > 850:          # skip the main LyX window (wide)
                    continue
                # Small LyX window — likely the reload message box
                children = []
                cb_ch = WinProc(lambda c, _: children.append(c) or True)
                user32.EnumChildWindows(hwnd, cb_ch, 0)
                for child in children:
                    if not user32.IsWindowVisible(child):
                        continue
                    if "button" not in _cls(child).lower():
                        continue
                    user32.SendMessageW(child, BM_CLICK, 0, 0)
                    log(f"Auto-dismissed LyX reload dialog for {Path(filepath).name}")
                    clicked = True
                    break
                if clicked:
                    break
            if clicked:
                break
    except Exception as e:
        log(f"LyX reload-dialog watcher error: {e}", "WARNING")


def start_lyx_reload_watcher(filepath):
    """Launch the background dialog-watcher after a merge-on-save write."""
    threading.Thread(
        target=_auto_reload_lyx_worker,
        args=(filepath,),
        daemon=True,
    ).start()


# ---------------------------------------------------------------------------
# Lock lifecycle
# ---------------------------------------------------------------------------

def create_lock(filepath):
    lock_file = Path(f"{filepath}{LOCK_SUFFIX}")
    if not lock_file.exists():
        write_lock_data(lock_file)
        state["my_locks"].add(filepath)
        create_baseline(filepath)
        try:
            state["file_mtimes"][filepath] = Path(filepath).stat().st_mtime
        except Exception:
            pass
        log(f"Lock created: {Path(filepath).name}")


def remove_lock(filepath):
    lock_file = Path(f"{filepath}{LOCK_SUFFIX}")
    if lock_file.exists():
        try:
            lock_file.unlink()
        except Exception:
            pass
    state["my_locks"].discard(filepath)
    state["file_change_pending"].pop(filepath, None)
    log(f"Lock removed: {Path(filepath).name}")

    if filepath in state["pending_merges"]:
        remote_backup = state["pending_merges"][filepath]
        local_backup = Path(f"{filepath}.local_version")
        try:
            shutil.copy2(filepath, local_backup)
            shutil.copy2(remote_backup, filepath)
            status, message = merge_files(filepath, str(local_backup))
            if status == "success":
                notify("LyX Sync - Merge Successful", f"{Path(filepath).name}:\n{message}")
                try:
                    Path(remote_backup).unlink()
                    local_backup.unlink()
                except Exception:
                    pass
            elif status == "conflict":
                notify("LyX Sync - Merge Conflicts",
                       f"{Path(filepath).name}:\n{message}\n\nPlease resolve manually.")
            else:
                notify("LyX Sync - Merge Error", f"{Path(filepath).name}:\n{message}")
        except Exception as e:
            notify("LyX Sync - Merge Error", f"Could not merge {Path(filepath).name}:\n{e}")
        state["pending_merges"].pop(filepath, None)

    state["file_mtimes"].pop(filepath, None)
    remove_baseline(filepath)


def scan_all_locks():
    locks = {}
    for d in state["watch_dirs"]:
        try:
            for lock_file in Path(d).rglob(f"*{LOCK_SUFFIX}"):
                original = str(lock_file)[: -len(LOCK_SUFFIX)]
                if Path(original).exists():
                    user, heartbeat, pid = read_lock_data(lock_file)
                    if is_stale_lock(lock_file):
                        user = f"{user} (stale?)"
                    locks[original] = user
        except Exception:
            pass
    return locks


# ---------------------------------------------------------------------------
# Heartbeat loop  — keeps our own lock files fresh every 30 s
# ---------------------------------------------------------------------------

def heartbeat_loop():
    while state["running"]:
        time.sleep(LOCK_HEARTBEAT_INTERVAL)
        for filepath in list(state["my_locks"]):
            lock_file = Path(f"{filepath}{LOCK_SUFFIX}")
            if lock_file.exists():
                try:
                    data = json.loads(lock_file.read_text())
                    data["heartbeat"] = time.time()
                    lock_file.write_text(json.dumps(data))
                except Exception:
                    try:
                        write_lock_data(lock_file)
                    except Exception:
                        pass


# ---------------------------------------------------------------------------
# Tray icon update
# ---------------------------------------------------------------------------

def update_tray():
    if not state["icon"]:
        return
    others = {k: v for k, v in state["locked_files"].items() if k not in state["my_locks"]}
    if others:
        color = "red"
        names = ", ".join(Path(f).name for f in others)
        tip = f"DropLyx — Locked by others: {names}"
    elif state["my_locks"]:
        color = "green"
        names = ", ".join(Path(f).name for f in state["my_locks"])
        tip = f"DropLyx — You editing: {names}"
    else:
        color = "lightblue"
        tip = "DropLyx — Monitoring, no files open"
    state["icon"].icon = create_icon(color)
    state["icon"].title = tip


# ---------------------------------------------------------------------------
# Main monitor loop
# ---------------------------------------------------------------------------

def monitor_loop():
    prev_locks = {}

    while state["running"]:
        loop_start = time.time()
        time.sleep(POLL_INTERVAL)

        detect_start = time.time()
        open_files = get_lyx_open_files()
        detect_time = time.time() - detect_start

        lock_start = time.time()
        for f in open_files:
            if f not in state["my_locks"]:
                create_lock(f)
        for f in list(state["my_locks"]):
            if f not in open_files:
                remove_lock(f)
        lock_time = time.time() - lock_start

        total_time = time.time() - loop_start

        # Log timing every 60 seconds
        if int(time.time()) % 60 < 1:
            log(f"Loop: {total_time:.2f}s (detect: {detect_time:.2f}s, "
                f"locks: {lock_time:.2f}s) open files: {len(open_files)}")

        state["locked_files"] = scan_all_locks()

        for f, user in state["locked_files"].items():
            if f not in prev_locks and f not in state["my_locks"]:
                notify("LyX Sync", f"{Path(f).name} locked by {user}")

        for f in prev_locks:
            if f not in state["locked_files"] and f not in state["my_locks"]:
                notify("LyX Sync", f"{Path(f).name} unlocked")

        # Check for remote changes with 2-second debounce (avoids reacting mid-sync)
        for filepath in list(state["my_locks"]):
            baseline_path = Path(f"{filepath}{BASELINE_SUFFIX}")
            if baseline_path.exists() and Path(filepath).exists():
                current_hash = compute_file_hash(filepath)
                last_hash = state["file_hashes"].get(filepath)

                if current_hash and last_hash and current_hash != last_hash:
                    pending = state["file_change_pending"].get(filepath)
                    if pending is None:
                        # Start debounce timer
                        state["file_change_pending"][filepath] = (current_hash, time.time())
                    elif pending[0] == current_hash:
                        # Same change — check if it has been stable long enough
                        if time.time() - pending[1] >= SYNC_DEBOUNCE_SECS:
                            state["file_change_pending"].pop(filepath, None)
                            remote_backup = Path(f"{filepath}.remote_version")
                            try:
                                shutil.copy2(filepath, remote_backup)
                                state["pending_merges"][filepath] = str(remote_backup)
                                state["file_hashes"][filepath] = current_hash
                                notify("LyX Sync - Remote Changes!",
                                       f"{Path(filepath).name} modified by another user.\n"
                                       "Changes will be merged when you close the file.")
                                log(f"Remote change detected (after debounce): {Path(filepath).name}")
                            except Exception as e:
                                notify("LyX Sync - Merge Error",
                                       f"Could not prepare merge for {Path(filepath).name}:\n{e}")
                    else:
                        # Hash changed again — reset debounce
                        state["file_change_pending"][filepath] = (current_hash, time.time())
                else:
                    state["file_change_pending"].pop(filepath, None)

        # Merge-on-save
        if state.get("merge_on_save", False):
            for filepath in list(state["my_locks"]):
                if Path(filepath).exists():
                    try:
                        current_mtime = Path(filepath).stat().st_mtime
                        last_mtime = state["file_mtimes"].get(filepath)
                        if last_mtime is not None and current_mtime > last_mtime:
                            if filepath in state["pending_merges"]:
                                perform_merge_on_save(filepath)
                            state["file_mtimes"][filepath] = current_mtime
                    except Exception:
                        pass

        # Dropbox conflict files
        for watch_dir in state.get("watch_dirs", []):
            try:
                for lyx_file in Path(watch_dir).rglob("*.lyx"):
                    if is_dropbox_conflict_file(str(lyx_file)):
                        conflict_key = f"{lyx_file}:{lyx_file.stat().st_mtime}"
                        if conflict_key not in state["processed_conflicts"]:
                            state["processed_conflicts"].add(conflict_key)
                            threading.Thread(
                                target=handle_dropbox_conflict,
                                args=(str(lyx_file),),
                                daemon=True,
                            ).start()
            except Exception:
                pass

        if len(state["processed_conflicts"]) > 100:
            state["processed_conflicts"] = set(list(state["processed_conflicts"])[-50:])

        prev_locks = dict(state["locked_files"])
        update_tray()


# ---------------------------------------------------------------------------
# Web log viewer  (local HTTP server, opened from tray)
# ---------------------------------------------------------------------------

class LogViewerHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress default HTTP server output

    def do_GET(self):
        if self.path in ("/", "/log"):
            self._serve_log()
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_log(self):
        try:
            content = LOG_FILE.read_text(encoding="utf-8", errors="replace")
            log_text = html_module.escape("\n".join(content.splitlines()[-1000:]))
        except Exception:
            log_text = "Log file not found or empty."

        refresh_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        body = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="10">
  <title>DropLyx Log</title>
  <style>
    *{{box-sizing:border-box}}
    body{{margin:0;padding:20px;background:#1e1e1e;color:#d4d4d4;
         font-family:'Consolas','Courier New',monospace}}
    h1{{color:#569cd6;margin-bottom:4px;font-size:1.4em}}
    .meta{{color:#888;font-size:12px;margin-bottom:12px}}
    pre{{background:#252526;padding:16px;border-radius:6px;overflow-x:auto;
         white-space:pre-wrap;word-break:break-all;font-size:13px;line-height:1.6;
         border:1px solid #333;max-height:calc(100vh - 120px);overflow-y:auto}}
  </style>
</head>
<body>
  <h1>DropLyx Log</h1>
  <div class="meta">Last 1000 lines &bull; Auto-refreshes every 10 s &bull; {refresh_ts}</div>
  <pre id="log">{log_text}</pre>
  <script>var e=document.getElementById('log');e.scrollTop=e.scrollHeight;</script>
</body>
</html>"""
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start_log_server():
    """Start the log viewer HTTP server on a random local port. Returns the port."""
    server = HTTPServer(("127.0.0.1", 0), LogViewerHandler)
    port = server.server_address[1]
    state["log_server_port"] = port
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"Log viewer at http://127.0.0.1:{port}")
    return port


def on_open_log(icon, item):
    port = state.get("log_server_port")
    if port:
        webbrowser.open(f"http://127.0.0.1:{port}")
    else:
        notify("DropLyx", "Log viewer unavailable")


# ---------------------------------------------------------------------------
# Optional startup on login
# ---------------------------------------------------------------------------

def get_exe_path():
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    return Path(sys.argv[0]).resolve()


def set_start_on_login(enable):
    """Enable or disable auto-start on login for the current platform."""
    try:
        if sys.platform == "win32":
            import winreg
            key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                                winreg.KEY_SET_VALUE) as key:
                if enable:
                    winreg.SetValueEx(key, "DropLyx", 0, winreg.REG_SZ, str(get_exe_path()))
                else:
                    try:
                        winreg.DeleteValue(key, "DropLyx")
                    except FileNotFoundError:
                        pass
        elif sys.platform == "darwin":
            plist_path = Path.home() / "Library/LaunchAgents/com.droplyx.app.plist"
            if enable:
                exe = get_exe_path()
                plist_path.write_text(
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                    "<plist version=\"1.0\"><dict>"
                    "<key>Label</key><string>com.droplyx.app</string>"
                    f"<key>ProgramArguments</key><array><string>{exe}</string></array>"
                    "<key>RunAtLoad</key><true/>"
                    "<key>KeepAlive</key><false/>"
                    "</dict></plist>"
                )
            else:
                plist_path.unlink(missing_ok=True)
        else:
            desktop_path = Path.home() / ".config/autostart/droplyx.desktop"
            if enable:
                exe = get_exe_path()
                desktop_path.parent.mkdir(parents=True, exist_ok=True)
                desktop_path.write_text(
                    "[Desktop Entry]\nType=Application\nName=DropLyx\n"
                    f"Exec={exe}\nHidden=false\nX-GNOME-Autostart-enabled=true\n"
                )
            else:
                desktop_path.unlink(missing_ok=True)
        return True
    except Exception as e:
        log(f"set_start_on_login({enable}) failed: {e}", "ERROR")
        return False


def get_start_on_login_state():
    """Return True if auto-start on login is currently enabled."""
    try:
        if sys.platform == "win32":
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run",
                                0, winreg.KEY_READ) as key:
                winreg.QueryValueEx(key, "DropLyx")
                return True
        elif sys.platform == "darwin":
            return (Path.home() / "Library/LaunchAgents/com.droplyx.app.plist").exists()
        else:
            return (Path.home() / ".config/autostart/droplyx.desktop").exists()
    except Exception:
        return False


def on_toggle_start_on_login(icon, item):
    new_state = not get_start_on_login_state()
    if set_start_on_login(new_state):
        state["start_on_login"] = new_state
        save_config()
        status = "enabled" if new_state else "disabled"
        notify("DropLyx", f"Start on login {status}")
        log(f"Start on login {status}")
    else:
        notify("DropLyx", "Could not change startup setting")
    state["menu_needs_update"] = True


# ---------------------------------------------------------------------------
# Tray menu actions
# ---------------------------------------------------------------------------

def on_status(icon, item):
    parts = [f"Watching {len(state['watch_dirs'])} folder(s)"]
    if state["my_locks"]:
        parts.append("You: " + ", ".join(Path(f).name for f in state["my_locks"]))
    others = {k: v for k, v in state["locked_files"].items() if k not in state["my_locks"]}
    if others:
        parts.append("Others: " + ", ".join(f"{Path(k).name} ({v})"
                                             for k, v in others.items()))
    if len(parts) == 1:
        parts.append("No files open")
    notify("LyX Sync Status", "\n".join(parts))


def add_folder_prompt():
    if sys.platform == "win32":
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        path = filedialog.askdirectory(title="Select folder to watch")
        root.destroy()
    else:
        import subprocess
        try:
            result = subprocess.run(
                ["zenity", "--file-selection", "--directory",
                 "--title=Select folder to watch"],
                capture_output=True, text=True, timeout=30,
            )
            path = result.stdout.strip()
        except Exception:
            path = input("Enter path to watch: ").strip().strip('"').strip("'")

    if path and Path(path).exists() and path not in state["watch_dirs"]:
        state["watch_dirs"].append(path)
        save_config()
        state["menu_needs_update"] = True
        notify("LyX Sync", f"Now watching: {path}")
        log(f"Added watch dir: {path}")
    elif path and path in state["watch_dirs"]:
        notify("LyX Sync", "Already watching this folder")


def on_add_folder(icon, item):
    threading.Thread(target=add_folder_prompt, daemon=True).start()


def make_remove_callback(path):
    def on_remove(icon, item):
        if path in state["watch_dirs"]:
            state["watch_dirs"].remove(path)
            save_config()
            state["menu_needs_update"] = True
            notify("LyX Sync", f"Removed: {path}")
            log(f"Removed watch dir: {path}")
    return on_remove


def on_toggle_merge_on_save(icon, item):
    state["merge_on_save"] = not state.get("merge_on_save", False)
    save_config()
    status = "enabled" if state["merge_on_save"] else "disabled"
    notify("Merge on Save", f"Merge on save is now {status}")
    state["menu_needs_update"] = True


def on_quit(icon, item):
    log("DropLyx quitting")
    for f in list(state["my_locks"]):
        remove_lock(f)
    state["running"] = False
    cleanup_pid_file()
    icon.stop()


def build_menu():
    items = [
        pystray.MenuItem("Status", on_status),
        pystray.MenuItem("Open Log", on_open_log),
        pystray.MenuItem("Add folder...", on_add_folder),
        pystray.Menu.SEPARATOR,
    ]
    if state["watch_dirs"]:
        items.append(pystray.MenuItem("Watching:", None, enabled=False))
        for d in state["watch_dirs"]:
            short = str(d) if len(str(d)) < 45 else "..." + str(d)[-42:]
            items.append(pystray.MenuItem(f"  x {short}", make_remove_callback(d)))
        items.append(pystray.Menu.SEPARATOR)

    items.append(pystray.MenuItem(
        "Merge on Save",
        on_toggle_merge_on_save,
        checked=lambda _: state.get("merge_on_save", False),
    ))
    items.append(pystray.MenuItem(
        "Start on Login",
        on_toggle_start_on_login,
        checked=lambda _: get_start_on_login_state(),
    ))
    items.append(pystray.Menu.SEPARATOR)
    items.append(pystray.MenuItem("Quit", on_quit))
    return tuple(items)


def menu_updater():
    while state["running"]:
        time.sleep(1)
        if state["menu_needs_update"] and state["icon"]:
            state["icon"].menu = build_menu()
            state["menu_needs_update"] = False


# ---------------------------------------------------------------------------
# First-run path prompt
# ---------------------------------------------------------------------------

def prompt_initial_path():
    if sys.platform == "win32":
        import tkinter as tk
        from tkinter import filedialog, messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(
            "LyX Sync - First Run",
            "Welcome to LyX Sync!\n\nPlease select the first folder to watch\n"
            "(e.g., your Dropbox folder with .lyx files)",
        )
        path = filedialog.askdirectory(title="Select folder to watch")
        root.destroy()
    else:
        import subprocess
        try:
            subprocess.run(
                ["zenity", "--info",
                 "--text=Welcome to LyX Sync! Please select the first folder to watch"],
                timeout=5,
            )
            result = subprocess.run(
                ["zenity", "--file-selection", "--directory",
                 "--title=Select folder to watch"],
                capture_output=True, text=True, timeout=30,
            )
            path = result.stdout.strip()
        except Exception:
            path = ""

    if not path or not Path(path).exists():
        if sys.platform == "win32":
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("LyX Sync", "No valid folder selected. Exiting.")
            root.destroy()
        sys.exit(1)
    return path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    setup_logging()

    if not check_single_instance():
        log("Another instance is already running. Exiting.", "WARNING")
        if sys.platform == "win32":
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showwarning("DropLyx", "DropLyx is already running.")
            root.destroy()
        sys.exit(0)

    dirs, merge_on_save, start_on_login = load_config()

    if len(sys.argv) > 1:
        dirs = [p for p in sys.argv[1:] if Path(p).exists()]
    elif not dirs:
        path = prompt_initial_path()
        dirs = [path]

    state["watch_dirs"] = dirs
    state["merge_on_save"] = merge_on_save
    state["start_on_login"] = start_on_login
    save_config()

    # Remove any stale locks we left from a previous crashed instance
    cleanup_stale_own_locks()

    # Start web log viewer (random local port)
    start_log_server()

    notify("LyX Sync Started", f"Monitoring {len(dirs)} folder(s)")

    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=menu_updater, daemon=True).start()
    threading.Thread(target=heartbeat_loop, daemon=True).start()

    icon = pystray.Icon("DropLyx", create_icon("lightblue"), "DropLyx", menu=build_menu())
    state["icon"] = icon
    icon.run()

    # Reached when icon.run() returns (quit via tray)
    cleanup_pid_file()
    log("DropLyx exited cleanly")


if __name__ == "__main__":
    main()

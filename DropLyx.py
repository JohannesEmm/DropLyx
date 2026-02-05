import os
import sys
import time
import json
import threading
import shutil
import hashlib
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
LOCK_SUFFIX = ".lock"
BASELINE_SUFFIX = ".baseline"
POLL_INTERVAL = 1  # Check every 1 second for faster response

state = {
    "watch_dirs": [],
    "locked_files": {},
    "my_locks": set(),
    "file_baselines": {},  # {filepath: baseline_path}
    "file_hashes": {},  # {filepath: last_known_hash}
    "pending_merges": {},  # {filepath: remote_backup_path}
    "running": True,
    "icon": None,
    "menu_needs_update": False,
    "window_cache": [],  # Cache of windows to avoid slow getAllWindows()
    "window_cache_time": 0,  # Last time windows were cached
    "window_cache_ttl": 5,  # Cache windows for 5 seconds
}


def notify(title, message):
    if HAS_PLYER:
        try:
            plyer_notif.notify(title=title, message=message, timeout=4)
        except:
            pass


def get_resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller"""
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = Path(sys._MEIPASS)
    except:
        base_path = Path(__file__).parent
    return base_path / relative_path


def create_icon(color="lightblue"):
    size = 64
    colors = {
        "lightblue": (52, 152, 219, 255),  # Light blue - nothing open
        "green": (46, 204, 113, 255),       # Green - editing files
        "red": (231, 76, 60, 255),          # Red - locked by others
        "orange": (243, 156, 18, 255)       # Orange - warning
    }
    status_color = colors.get(color, colors["lightblue"])

    # Try to load the DropLyx logo
    try:
        logo_path = get_resource_path("DropLyx_logo.png")
        img = Image.open(logo_path)

        # Convert to RGBA if needed
        if img.mode != 'RGBA':
            img = img.convert('RGBA')

        # Resize to icon size
        img = img.resize((size, size), Image.Resampling.LANCZOS)

        # Create a copy to draw on
        img = img.copy()
        draw = ImageDraw.Draw(img)

        # Add a colored status indicator circle in the bottom-right corner
        indicator_size = 20
        indicator_x = size - indicator_size - 2
        indicator_y = size - indicator_size - 2

        # Draw the status indicator with a white border
        draw.ellipse(
            [indicator_x - 2, indicator_y - 2,
             indicator_x + indicator_size + 2, indicator_y + indicator_size + 2],
            fill=(255, 255, 255, 255),  # White border
            outline=None
        )
        draw.ellipse(
            [indicator_x, indicator_y,
             indicator_x + indicator_size, indicator_y + indicator_size],
            fill=status_color,
            outline=None
        )

        return img

    except Exception as e:
        # Fallback: create simple colored circle with text
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([2, 2, size - 2, size - 2], fill=status_color, outline=(255, 255, 255), width=2)
        try:
            font = ImageFont.truetype("arial.ttf", 28)
        except:
            font = ImageFont.load_default()
        draw.text((20, 18), "D", fill=(255, 255, 255), font=font)
        return img


def save_config():
    CONFIG_FILE.write_text(json.dumps({"watch_dirs": state["watch_dirs"]}))


def load_config():
    if CONFIG_FILE.exists():
        data = json.loads(CONFIG_FILE.read_text())
        if "watch_dir" in data and "watch_dirs" not in data:
            return [data["watch_dir"]]
        return data.get("watch_dirs", [])
    return []


def get_username():
    return os.getenv("USER") or os.getenv("USERNAME") or "unknown"


def get_lyx_open_files():
    open_files = []

    if sys.platform == "win32":
        # Method 1: Fast process-based detection (check command line and open files)
        # This is much faster than window enumeration
        for proc in psutil.process_iter(["name", "cmdline"]):
            try:
                proc_name = proc.info.get("name", "")
                if proc_name and "lyx" in proc_name.lower():
                    # Check command line arguments for .lyx files
                    cmdline = proc.info.get("cmdline") or []
                    for arg in cmdline:
                        if arg and isinstance(arg, str) and arg.endswith(".lyx"):
                            filepath = Path(arg)
                            if filepath.exists():
                                # Check if file is in one of our watched directories
                                for watch_dir in state.get("watch_dirs", []):
                                    try:
                                        filepath.resolve().relative_to(Path(watch_dir).resolve())
                                        open_files.append(str(filepath.resolve()))
                                        break
                                    except ValueError:
                                        # Not in this watched directory
                                        continue

                    # Method 2: Check open file handles (requires elevated privileges, might fail)
                    try:
                        open_file_objs = proc.open_files()
                        for f in open_file_objs:
                            if hasattr(f, 'path') and f.path.endswith(".lyx"):
                                filepath = Path(f.path)
                                # Check if in watched directories
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

        # Method 3: Window title detection (SLOW fallback - only if nothing found yet)
        # Only use this if process-based detection found nothing
        if not open_files:
            try:
                import pygetwindow as gw

                # Use cached windows if cache is fresh (< 5 seconds old)
                current_time = time.time()
                if (current_time - state["window_cache_time"]) < state["window_cache_ttl"]:
                    windows = state["window_cache"]
                else:
                    # Get all windows (this is VERY slow, so we cache it)
                    windows = gw.getAllWindows()
                    state["window_cache"] = windows
                    state["window_cache_time"] = current_time

                for window in windows:
                    title = window.title
                    # LyX windows have titles like "filename.lyx - LyX" or "newfile1.lyx (~\CMCC Dropbox\...) - LyX"
                    if title and "LyX" in title and ".lyx" in title:
                        # Extract the filename from the title
                        parts = title.split(" - LyX")[0]

                        # Check if there's a path in parentheses
                        if "(" in parts and ")" in parts:
                            filename = parts.split("(")[0].strip()
                            folder_path = parts[parts.find("(")+1:parts.find(")")]

                            # Handle Windows path starting with ~\
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
            except Exception as e:
                pass
    else:
        # Linux/Mac: use open files
        for proc in psutil.process_iter(["name", "open_files"]):
            try:
                if proc.info["name"] == "lyx":
                    for f in proc.info.get("open_files") or []:
                        if f.path.endswith(".lyx"):
                            open_files.append(f.path)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    return list(set(open_files))  # Remove duplicates


def compute_file_hash(filepath):
    """Compute SHA256 hash of file content"""
    try:
        with open(filepath, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()
    except:
        return None


def create_baseline(filepath):
    """Create baseline copy when starting to edit"""
    baseline_path = Path(f"{filepath}{BASELINE_SUFFIX}")
    try:
        shutil.copy2(filepath, baseline_path)
        state["file_baselines"][filepath] = str(baseline_path)
        state["file_hashes"][filepath] = compute_file_hash(filepath)
        return True
    except Exception as e:
        return False


def remove_baseline(filepath):
    """Remove baseline when done editing"""
    baseline_path = Path(f"{filepath}{BASELINE_SUFFIX}")
    if baseline_path.exists():
        try:
            baseline_path.unlink()
        except:
            pass
    state["file_baselines"].pop(filepath, None)
    state["file_hashes"].pop(filepath, None)


def detect_conflicts(baseline_lines, local_lines, remote_lines):
    """
    Detect conflicts between local and remote changes.
    Returns: (has_conflicts, conflicting_line_numbers)
    """
    conflicts = []

    # Find lines that changed in both versions
    for i in range(max(len(baseline_lines), len(local_lines), len(remote_lines))):
        baseline_line = baseline_lines[i] if i < len(baseline_lines) else ""
        local_line = local_lines[i] if i < len(local_lines) else ""
        remote_line = remote_lines[i] if i < len(remote_lines) else ""

        # Both changed the same line differently
        if baseline_line != local_line and baseline_line != remote_line:
            if local_line != remote_line:
                conflicts.append(i)

    return len(conflicts) > 0, conflicts


def merge_files(filepath, local_version_path=None):
    """
    Attempt to merge changes from remote file with local changes.
    Uses 3-way merge: baseline vs local vs remote
    Returns: ('success', 'conflict', or 'error', message)
    """
    baseline_path = Path(f"{filepath}{BASELINE_SUFFIX}")

    # Check if we have a baseline
    if not baseline_path.exists():
        return ('error', 'No baseline found')

    try:
        # Read baseline
        with open(baseline_path, 'r', encoding='utf-8', errors='replace') as f:
            baseline_lines = f.readlines()

        # Read remote (current file on disk)
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            remote_lines = f.readlines()

        # Read local version if provided, otherwise use baseline as local
        # (means we haven't made changes yet)
        if local_version_path and Path(local_version_path).exists():
            with open(local_version_path, 'r', encoding='utf-8', errors='replace') as f:
                local_lines = f.readlines()
        else:
            # No local changes yet, so local = baseline
            local_lines = baseline_lines

        # Check if remote actually changed
        if remote_lines == baseline_lines:
            return ('success', 'No remote changes detected')

        # Check if local changed
        if local_lines == baseline_lines:
            # We haven't made local changes, so just accept remote
            return ('success', 'No local changes - accepting remote version')

        # Both changed - need to merge
        # Detect conflicts
        has_conflicts, conflict_lines = detect_conflicts(baseline_lines, local_lines, remote_lines)

        if has_conflicts:
            # Create backups for manual resolution
            backup_remote = Path(f"{filepath}.remote_backup")
            backup_local = Path(f"{filepath}.local_backup")

            shutil.copy2(filepath, backup_remote)
            if local_version_path:
                shutil.copy2(local_version_path, backup_local)

            return ('conflict',
                    f'Conflicts detected at {len(conflict_lines)} line(s).\n'
                    f'Backups created:\n{backup_remote.name}\n{backup_local.name}')

        # No conflicts - perform merge
        merged_lines = []
        max_len = max(len(baseline_lines), len(local_lines), len(remote_lines))

        for i in range(max_len):
            baseline_line = baseline_lines[i] if i < len(baseline_lines) else None
            local_line = local_lines[i] if i < len(local_lines) else None
            remote_line = remote_lines[i] if i < len(remote_lines) else None

            # Decide which line to use
            if local_line == remote_line:
                # Both made same change or both unchanged
                if local_line is not None:
                    merged_lines.append(local_line)
            elif local_line == baseline_line:
                # Only remote changed this line
                if remote_line is not None:
                    merged_lines.append(remote_line)
            elif remote_line == baseline_line:
                # Only local changed this line
                if local_line is not None:
                    merged_lines.append(local_line)
            else:
                # Both changed but we already checked for conflicts
                # This shouldn't happen, but default to local
                if local_line is not None:
                    merged_lines.append(local_line)
                elif remote_line is not None:
                    merged_lines.append(remote_line)

        # Create backup before merging
        backup_path = Path(f"{filepath}.pre_merge_backup")
        shutil.copy2(filepath, backup_path)

        # Write merged content
        with open(filepath, 'w', encoding='utf-8') as f:
            f.writelines(merged_lines)

        return ('success',
                f'Successfully merged changes.\n'
                f'Backup saved as: {backup_path.name}')

    except Exception as e:
        return ('error', f'Merge error: {str(e)}')


def create_lock(filepath):
    lock_file = Path(f"{filepath}{LOCK_SUFFIX}")
    if not lock_file.exists():
        lock_file.write_text(get_username())
        state["my_locks"].add(filepath)
        # Create baseline for merge tracking
        create_baseline(filepath)


def remove_lock(filepath):
    lock_file = Path(f"{filepath}{LOCK_SUFFIX}")
    if lock_file.exists():
        try:
            lock_file.unlink()
        except:
            pass
    state["my_locks"].discard(filepath)

    # Check if there's a pending merge
    if filepath in state["pending_merges"]:
        remote_backup = state["pending_merges"][filepath]

        # Now we have:
        # - Baseline: original file
        # - Remote: the remote_backup file (changes from other user)
        # - Local: current file (our changes)

        # Save our current version as local backup
        local_backup = Path(f"{filepath}.local_version")
        try:
            shutil.copy2(filepath, local_backup)

            # Copy remote version back to main file for merging
            shutil.copy2(remote_backup, filepath)

            # Attempt merge
            status, message = merge_files(filepath, str(local_backup))

            if status == 'success':
                notify("LyX Sync - Merge Successful",
                       f"{Path(filepath).name}:\n{message}")
                # Clean up backup files
                try:
                    Path(remote_backup).unlink()
                    local_backup.unlink()
                except:
                    pass
            elif status == 'conflict':
                notify("LyX Sync - Merge Conflicts",
                       f"{Path(filepath).name}:\n{message}\n\n"
                       f"Please review and resolve conflicts manually.")
            else:
                notify("LyX Sync - Merge Error",
                       f"{Path(filepath).name}:\n{message}")

        except Exception as e:
            notify("LyX Sync - Merge Error",
                   f"Could not merge {Path(filepath).name}:\n{str(e)}")

        # Remove from pending merges
        state["pending_merges"].pop(filepath, None)

    # Remove baseline when done editing
    remove_baseline(filepath)


def scan_all_locks():
    locks = {}
    for d in state["watch_dirs"]:
        for lock_file in Path(d).rglob(f"*{LOCK_SUFFIX}"):
            original = str(lock_file)[: -len(LOCK_SUFFIX)]
            if Path(original).exists():
                locks[original] = lock_file.read_text().strip()
    return locks


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


def monitor_loop():
    prev_locks = {}
    debug_log = Path.home() / "droplyx_timing.log"

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

        # Log timing every 10 loops
        if int(time.time()) % 10 < 1:
            with open(debug_log, 'a') as f:
                f.write(f"[{datetime.now().strftime('%H:%M:%S')}] Loop: {total_time:.2f}s (detect: {detect_time:.2f}s, locks: {lock_time:.2f}s) - Files: {len(open_files)}\n")

        state["locked_files"] = scan_all_locks()

        for f, user in state["locked_files"].items():
            if f not in prev_locks and f not in state["my_locks"]:
                notify("LyX Sync", f"{Path(f).name} locked by {user}")

        for f in prev_locks:
            if f not in state["locked_files"] and f not in state["my_locks"]:
                notify("LyX Sync", f"{Path(f).name} unlocked")

        # Check for remote changes on files we're editing
        for filepath in list(state["my_locks"]):
            baseline_path = Path(f"{filepath}{BASELINE_SUFFIX}")
            if baseline_path.exists() and Path(filepath).exists():
                # Check if file changed on disk
                current_hash = compute_file_hash(filepath)
                last_hash = state["file_hashes"].get(filepath)

                if current_hash and last_hash and current_hash != last_hash:
                    # File changed on disk while we're editing!
                    # This means someone else edited it and Dropbox synced it

                    # Save the remote version for merging later
                    remote_backup = Path(f"{filepath}.remote_version")
                    try:
                        shutil.copy2(filepath, remote_backup)
                        state["pending_merges"][filepath] = str(remote_backup)

                        notify("LyX Sync - Remote Changes!",
                               f"{Path(filepath).name} was modified by another user.\n"
                               f"Changes will be merged when you close the file.")

                        # Update the hash
                        state["file_hashes"][filepath] = current_hash

                    except Exception as e:
                        notify("LyX Sync - Merge Error",
                               f"Could not prepare merge for {Path(filepath).name}:\n{str(e)}")

        prev_locks = dict(state["locked_files"])
        update_tray()


def on_status(icon, item):
    parts = [f"Watching {len(state['watch_dirs'])} folder(s)"]
    if state["my_locks"]:
        parts.append("You: " + ", ".join(Path(f).name for f in state["my_locks"]))
    others = {k: v for k, v in state["locked_files"].items() if k not in state["my_locks"]}
    if others:
        parts.append("Others: " + ", ".join(f"{Path(k).name} ({v})" for k, v in others.items()))
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
                ["zenity", "--file-selection", "--directory", "--title=Select folder to watch"],
                capture_output=True, text=True, timeout=30
            )
            path = result.stdout.strip()
        except:
            path = input("Enter path to watch: ").strip().strip('"').strip("'")

    if path and Path(path).exists() and path not in state["watch_dirs"]:
        state["watch_dirs"].append(path)
        save_config()
        state["menu_needs_update"] = True
        notify("LyX Sync", f"Now watching: {path}")
    elif path and path in state["watch_dirs"]:
        notify("LyX Sync", "Already watching this folder")


def on_add_folder(icon, item):
    t = threading.Thread(target=add_folder_prompt, daemon=True)
    t.start()


def make_remove_callback(path):
    def on_remove(icon, item):
        if path in state["watch_dirs"]:
            state["watch_dirs"].remove(path)
            save_config()
            state["menu_needs_update"] = True
            notify("LyX Sync", f"Removed: {path}")
    return on_remove


def on_quit(icon, item):
    for f in list(state["my_locks"]):
        remove_lock(f)
    state["running"] = False
    icon.stop()


def build_menu():
    items = [
        pystray.MenuItem("Status", on_status),
        pystray.MenuItem("Add folder...", on_add_folder),
        pystray.Menu.SEPARATOR,
    ]
    if state["watch_dirs"]:
        items.append(pystray.MenuItem("Watching:", None, enabled=False))
        for d in state["watch_dirs"]:
            short = str(d) if len(str(d)) < 45 else "..." + str(d)[-42:]
            items.append(pystray.MenuItem(f"  x {short}", make_remove_callback(d)))
        items.append(pystray.Menu.SEPARATOR)
    items.append(pystray.MenuItem("Quit", on_quit))
    return tuple(items)


def menu_updater():
    while state["running"]:
        time.sleep(1)
        if state["menu_needs_update"] and state["icon"]:
            state["icon"].menu = build_menu()
            state["menu_needs_update"] = False


def prompt_initial_path():
    if sys.platform == "win32":
        import tkinter as tk
        from tkinter import filedialog, messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("LyX Sync - First Run", "Welcome to LyX Sync!\n\nPlease select the first folder to watch\n(e.g., your Dropbox folder with .lyx files)")
        path = filedialog.askdirectory(title="Select folder to watch")
        root.destroy()
    else:
        import subprocess
        try:
            result = subprocess.run(
                ["zenity", "--info", "--text=Welcome to LyX Sync! Please select the first folder to watch"],
                timeout=5
            )
            result = subprocess.run(
                ["zenity", "--file-selection", "--directory", "--title=Select folder to watch"],
                capture_output=True, text=True, timeout=30
            )
            path = result.stdout.strip()
        except:
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


def main():
    dirs = load_config()

    if len(sys.argv) > 1:
        dirs = [p for p in sys.argv[1:] if Path(p).exists()]
    elif not dirs:
        path = prompt_initial_path()
        dirs = [path]

    state["watch_dirs"] = dirs
    save_config()

    # Show initial notification
    notify("LyX Sync Started", f"Monitoring {len(dirs)} folder(s)")

    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=menu_updater, daemon=True).start()

    icon = pystray.Icon("DropLyx", create_icon("lightblue"), "DropLyx", menu=build_menu())
    state["icon"] = icon
    icon.run()


if __name__ == "__main__":
    main()

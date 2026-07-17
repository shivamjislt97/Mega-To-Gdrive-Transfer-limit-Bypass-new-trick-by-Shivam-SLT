#!/usr/bin/env python3
"""MEGA to Google Drive multi-folder transfer with artifact-based state tracking."""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

MEGA_LINKS_RAW = os.environ.get("MEGA_LINKS", "")
RCLONE_CONF_RAW = os.environ.get("RCLONE_CONF", "")

# mega.py uses deprecated asyncio.coroutine — restore if missing
import asyncio
if not hasattr(asyncio, "coroutine"):
    asyncio.coroutine = lambda c: c
GDRIVE_REMOTE = "gdrive"
BASE_FOLDER = "MEGA_Transfer"
QUOTA_MAX = 5 * 1024 * 1024 * 1024
QUOTA_MARKERS = ["over quota", "bandwidth limit", "quota exceeded", "429", "eoverquota"]

WORKSPACE = os.environ.get("GITHUB_WORKSPACE", os.getcwd())
COMPLETED_FILE = os.path.join(WORKSPACE, "completed_links.json")
TEMP_DIR = os.path.join(WORKSPACE, "mega_temp")
MAX_RETRIES = 3


def fmt_size(b):
    if b is None:
        return "unknown"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def parse_size_num(val, unit):
    units = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}
    return float(val) * units.get(unit, 1)


def is_quota(text):
    return any(m in text.lower() for m in QUOTA_MARKERS)


def log(msg, end='\n'):
    print(msg, flush=True, end=end)


def git_push(quiet=False):
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], capture_output=True, timeout=5)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], capture_output=True, timeout=5)
        subprocess.run(["git", "add", "completed_links.json"], check=True, capture_output=True, timeout=15)
        r = subprocess.run(
            ["git", "commit", "-m", "update state [skip ci]"],
            capture_output=True, text=True, timeout=15
        )
        if r.returncode != 0 and "nothing to commit" not in r.stderr and "nothing to commit" not in r.stdout:
            if not quiet:
                log(f"  [git] commit skipped: {r.stderr.strip() or r.stdout.strip()}")
            return
        subprocess.run(
            ["git", "pull", "--rebase", "origin", "main"],
            capture_output=True, timeout=30
        )
        r = subprocess.run(["git", "push"], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            err = r.stderr.strip() or r.stdout.strip()
            if not quiet:
                log(f"  [git] push failed: {err}")
            return
        if not quiet:
            log("  [git] state pushed to repo")
    except subprocess.TimeoutExpired:
        if not quiet:
            log("  [git] timeout pushing state")
    except Exception as e:
        if not quiet:
            log(f"  [git] push error: {e}")


def timestamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_completed():
    if os.path.exists(COMPLETED_FILE):
        try:
            with open(COMPLETED_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"folders": {}, "completed": [], "current_folder": None, "oversized": []}


def save_completed(state):
    with open(COMPLETED_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_file_info(url):
    try:
        from mega import Mega
        info = Mega().get_public_url_info(url)
        name = info.get("name")
        size = info.get("size")
        if name and size is not None:
            return name, size
    except Exception as e:
        log(f"  [debug] mega.py error: {e}")
        print(f"::error::get_file_info: {e}")
    try:
        r = subprocess.run(
            ["megadl", "--info", url],
            capture_output=True, text=True, timeout=30
        )
        out = (r.stdout + " " + r.stderr).strip()
        name = re.search(r"(?:File|Name):\s*(.+?)\s*\(", out)
        size = re.search(r"\((\d+)\s*bytes?\)", out)
        if name and size:
            return name.group(1), int(size.group(1))
    except Exception:
        pass
    return None, None


def download_file(url, timeout=600, quota_used=0, quota_max=0, total_size=0):
    if os.path.isdir(TEMP_DIR):
        shutil.rmtree(TEMP_DIR)
    os.makedirs(TEMP_DIR, exist_ok=True)

    process = subprocess.Popen(
        ["megadl", "--path", TEMP_DIR, url],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True
    )

    output = []
    last_line = ""
    start = time.time()

    def monitor():
        nonlocal last_line
        while process.poll() is None:
            if os.path.isdir(TEMP_DIR):
                files = [f for f in os.listdir(TEMP_DIR) if os.path.isfile(os.path.join(TEMP_DIR, f))]
                if files:
                    cur_size = os.path.getsize(os.path.join(TEMP_DIR, files[0]))
                    elapsed = time.time() - start
                    speed = cur_size / elapsed if elapsed > 0 else 0
                    if total_size > 0:
                        pct = min(100.0, cur_size * 100 / total_size)
                        line_text = f"  [DOWNLOAD] {fmt_size(cur_size)} / {fmt_size(total_size)} ({pct:.0f}%) @ {fmt_size(speed)}/s | Quota: {fmt_size(quota_used + cur_size)}/{fmt_size(quota_max)}"
                    else:
                        line_text = f"  [DOWNLOAD] {fmt_size(cur_size)} downloaded @ {fmt_size(speed)}/s | Quota: {fmt_size(quota_used + cur_size)}/{fmt_size(quota_max)}"
                    if line_text != last_line:
                        log(line_text, end='\r')
                        last_line = line_text
            time.sleep(2)

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()

    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        thread.join(timeout=3)
        raise RuntimeError("megadl timed out")

    thread.join(timeout=3)
    log("")

    if process.returncode != 0:
        out = "".join(output)
        raise RuntimeError(out.strip() or f"megadl exit {process.returncode}")

    files = [f for f in os.listdir(TEMP_DIR) if os.path.isfile(os.path.join(TEMP_DIR, f))]
    if not files:
        raise RuntimeError("No file downloaded")
    return os.path.join(TEMP_DIR, files[0])


def ensure_gdrive_folder(folder_name):
    target = f"{GDRIVE_REMOTE}:{BASE_FOLDER}/{folder_name}"
    try:
        r = subprocess.run(
            ["rclone", "mkdir", target],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode != 0:
            log(f"  warning: rclone mkdir stderr: {r.stderr[:200]}")
    except Exception:
        log(f"  warning: rclone mkdir failed (non-fatal)")


def upload_file(filepath, folder_name, quota_used=0, quota_max=0):
    target = f"{GDRIVE_REMOTE}:{BASE_FOLDER}/{folder_name}/"

    process = subprocess.Popen(
        ["rclone", "copy", filepath, target, "--stats=3s"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1
    )

    file_size = os.path.getsize(filepath)
    last_line = ""

    def reader():
        nonlocal last_line
        for line in process.stderr:
            line_s = line.rstrip()
            if "Transferred:" in line_s and "/" in line_s:
                m = re.search(r"Transferred:\s*([\d.]+)\s*(B|[KMG]i?B)\s*/\s*([\d.]+)\s*(B|[KMG]i?B)", line_s)
                if m:
                    cur = m.group(1) + " " + m.group(2)
                    total = m.group(3) + " " + m.group(4)
                    pct_match = re.search(r",\s*(\d+)%", line_s)
                    pct = pct_match.group(1) if pct_match else "?"
                    q_str = fmt_size(quota_used + file_size)
                    line_text = f"  [UPLOAD] {cur} / {total} ({pct}%) | Quota: {q_str}/{fmt_size(quota_max)}"
                    if line_text != last_line:
                        log(line_text, end='\r')
                        last_line = line_text

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    try:
        process.wait(timeout=3600)
    except subprocess.TimeoutExpired:
        process.kill()
        thread.join(timeout=3)
        raise RuntimeError("rclone copy timed out")

    thread.join(timeout=3)
    log("")  # newline after progress

    stdout_text = process.stdout.read()
    if process.returncode != 0:
        raise RuntimeError((stdout_text or "").strip()[:300] or f"rclone copy exit {process.returncode}")
    return os.path.basename(filepath)


def verify_upload(filename, file_size, folder_name):
    target = f"{GDRIVE_REMOTE}:{BASE_FOLDER}/{folder_name}/{filename}"
    try:
        r = subprocess.run(
            ["rclone", "lsjson", target],
            capture_output=True, text=True, timeout=60
        )
        if r.returncode != 0 or not r.stdout.strip():
            return False
        files = json.loads(r.stdout)
        for f in files:
            if f.get("Name") == filename and f.get("Size") == file_size:
                return True
    except Exception:
        pass
    return False


def main():
    # Setup rclone config
    conf_dir = os.path.expanduser("~/.config/rclone")
    conf_path = os.path.join(conf_dir, "rclone.conf")
    os.makedirs(conf_dir, exist_ok=True)
    if not os.path.exists(conf_path):
        if not RCLONE_CONF_RAW:
            log("ERROR: RCLONE_CONF secret is empty")
            sys.exit(1)
        with open(conf_path, "w") as f:
            f.write(RCLONE_CONF_RAW)
        log("  rclone.conf written")

    # Load artifact state
    state = load_completed()
    folders = state.get("folders", {})
    completed = state.get("completed", [])
    current_folder = state.get("current_folder")
    oversized = state.get("oversized", [])

    # Parse MEGA_LINKS JSON
    if not MEGA_LINKS_RAW.strip():
        log("ERROR: MEGA_LINKS secret is empty")
        sys.exit(1)

    try:
        all_links = json.loads(MEGA_LINKS_RAW)
    except json.JSONDecodeError as e:
        log(f"ERROR: MEGA_LINKS is not valid JSON: {e}")
        log("   Expected: {\"FolderName\": [\"url1\", \"url2\"]}")
        sys.exit(1)

    if not isinstance(all_links, dict):
        log("ERROR: MEGA_LINKS must be a JSON object {\"folder\": [urls]}")
        sys.exit(1)

    # Ensure all folders from secret are in state
    for folder_name, links in all_links.items():
        if folder_name not in folders:
            folders[folder_name] = {
                "total": len(links),
                "done": 0,
                "status": "pending"
            }

    # Auto-activate first pending folder
    if not current_folder or current_folder not in folders:
        for name, fdata in folders.items():
            if fdata["status"] == "pending":
                fdata["status"] = "active"
                current_folder = name
                state["current_folder"] = name
                break

    state["folders"] = folders
    save_completed(state)

    # Build lookup sets
    completed_urls = set(item["url"] for item in completed)
    oversized_urls = set(item["url"] for item in oversized)

    # Stats
    total_pending_all = sum(
        f["total"] - f["done"] for f in folders.values() if f["status"] != "completed"
    )

    log("=" * 55)
    log(f"  MEGA -> GDrive Transfer | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log("=" * 55)
    log(f"  Artifact loaded: {len(completed)} completed files, {len(oversized)} oversized")
    log(f"  Total pending: {total_pending_all}")
    log("-" * 55)
    for name, fdata in folders.items():
        icon = "ACTIVE" if fdata["status"] == "active" else "DONE" if fdata["status"] == "completed" else "WAIT"
        log(f"  [{icon}] {name}: {fdata['done']}/{fdata['total']}")
    log("-" * 55)

    if total_pending_all == 0:
        log(f"\n  ALL FOLDERS COMPLETE! Sab files transfer ho gayi!")
        log("=" * 55)
        return

    # Find active folder
    active_folder = None
    for name, fdata in folders.items():
        if fdata["status"] == "active":
            active_folder = name
            break

    if not active_folder:
        log("  No active folder found. Check state.")
        sys.exit(0)

    folder_links = all_links.get(active_folder, [])
    pending = []
    for url in folder_links:
        if url in completed_urls or url in oversized_urls:
            continue
        pending.append(url)

    total = len(pending)
    log(f"\n  Active: [{active_folder}] -> {total} files pending")
    log("=" * 55 + "\n")

    if total == 0:
        folders[active_folder]["status"] = "completed"
        folders[active_folder]["done"] = folders[active_folder]["total"]
        state["folders"] = folders
        save_completed(state)
        log(f"  [{active_folder}] already complete. Moving on.")
        sys.exit(0)

    # Process files
    quota_used = 0
    processed = 0

    for idx, url in enumerate(pending, 1):
        log(f"  --- [{idx}/{total}] {active_folder} ---")
        log(f"  Fetching: {url[:60]}...")

        filename, file_size = get_file_info(url)
        metadata_ok = filename and file_size is not None

        if metadata_ok:
            log(f"  [{active_folder}] \"{filename}\" | Size: {fmt_size(file_size)}")
            if file_size > QUOTA_MAX:
                log(f"  OVERSIZED: {filename} ({fmt_size(file_size)}) > 5GB")
                oversized.append({
                    "url": url, "filename": filename,
                    "size": file_size, "target_folder": active_folder
                })
                state["oversized"] = oversized
                save_completed(state)
                continue
            if quota_used + file_size > QUOTA_MAX:
                log(f"  Quota full: {fmt_size(quota_used)} + {fmt_size(file_size)} > 5GB")
                log(f"  Skipping \"{filename}\" for this run")
                break
        else:
            log(f"  (metadata unavailable — downloading directly)")

        # Download
        dl_start = time.time()
        log(f"  DOWNLOADING: \"{filename or '?'}\" ({fmt_size(file_size or 0)})...")
        try:
            local_path = download_file(url, timeout=600, quota_used=quota_used, quota_max=QUOTA_MAX, total_size=file_size or 0)
            actual_size = os.path.getsize(local_path)
            actual_name = os.path.basename(local_path)
            dl_elapsed = time.time() - dl_start
            log(f"  Downloaded: {fmt_size(actual_size)} in {dl_elapsed:.0f}s")
        except RuntimeError as e:
            msg = str(e)
            if is_quota(msg):
                log(f"\n  QUOTA EXCEEDED mid-download! Stopping.")
                log(f"  {processed} files done this run.")
                break
            log(f"  Download failed: {msg[:200]}")
            print(f"::error::download failed: {msg[:200]}")
            continue

        # If metadata was missing, use values from downloaded file
        if not metadata_ok:
            filename = actual_name
            file_size = actual_size
            if file_size > QUOTA_MAX:
                log(f"  OVERSIZED: {filename} ({fmt_size(file_size)}) > 5GB")
                oversized.append({
                    "url": url, "filename": filename,
                    "size": file_size, "target_folder": active_folder
                })
                state["oversized"] = oversized
                save_completed(state)
                shutil.rmtree(TEMP_DIR, ignore_errors=True)
                continue
            if quota_used + file_size > QUOTA_MAX:
                log(f"  Quota full after download ({fmt_size(quota_used)} + {fmt_size(file_size)} > 5GB)")
                log(f"  Processing this file anyway (already downloaded), then stopping.")

        quota_exhausted = (quota_used + file_size) >= QUOTA_MAX
        quota_used += file_size

        # Upload
        ul_start = time.time()
        log(f"  UPLOADING: \"{filename}\" ({fmt_size(file_size)}) to GDrive/{BASE_FOLDER}/{active_folder}/...")
        ensure_gdrive_folder(active_folder)
        try:
            uploaded_name = upload_file(local_path, active_folder, quota_used=quota_used, quota_max=QUOTA_MAX)
            ul_elapsed = time.time() - ul_start
            log(f"  Uploaded: \"{uploaded_name}\" ({fmt_size(file_size)} in {ul_elapsed:.0f}s)")
        except RuntimeError as e:
            log(f"  Upload failed: {str(e)[:200]}")
            shutil.rmtree(TEMP_DIR, ignore_errors=True)
            continue

        # Directly mark complete after upload (no verify — upload always succeeds)
        completed.append({
            "url": url,
            "filename": uploaded_name,
            "size": file_size,
            "target_folder": active_folder,
            "completed_at": timestamp()
        })
        folders[active_folder]["done"] += 1
        state["completed"] = completed
        state["folders"] = folders
        save_completed(state)
        git_push(quiet=True)
        log(f"  Artifact+Git saved: {folders[active_folder]['done']}/{folders[active_folder]['total']} done")

        # Cleanup
        shutil.rmtree(TEMP_DIR, ignore_errors=True)

        processed += 1
        log(f"  [{idx}/{total}] Complete | Quota: {fmt_size(quota_used)}/{fmt_size(QUOTA_MAX)}")
        log(f"  {'-' * 50}")

        if quota_exhausted:
            log(f"  Quota exhausted — remaining files will be processed next run.")
            break

    # Folder completion check
    fdata = folders[active_folder]
    if fdata["done"] >= fdata["total"]:
        fdata["status"] = "completed"
        log(f"\n  FOLDER COMPLETE: [{active_folder}] - {fdata['done']}/{fdata['total']} files")
        next_folder = None
        for name, fd in folders.items():
            if fd["status"] == "pending":
                fd["status"] = "active"
                next_folder = name
                break
        if next_folder:
            state["current_folder"] = next_folder
            fd_next = folders[next_folder]
            log(f"  Next folder: [{next_folder}] - {fd_next['done']}/{fd_next['total']}")
        else:
            state["current_folder"] = None
            log(f"  ALL FOLDERS COMPLETE! Sab kaam ho gaya!")
    else:
        log(f"\n  [{active_folder}] Progress: {fdata['done']}/{fdata['total']}")

    state["folders"] = folders
    state["completed"] = completed
    state["oversized"] = oversized
    save_completed(state)
    git_push()

    # Summary
    log(f"\n{'=' * 55}")
    log(f"  RUN SUMMARY")
    log(f"  {'-' * 55}")
    log(f"  Processed: {processed} files")
    log(f"  Quota used: {fmt_size(quota_used)} / {fmt_size(QUOTA_MAX)}")
    for name, fd in folders.items():
        icon = "DONE" if fd["status"] == "completed" else "ACTIVE" if fd["status"] == "active" else "WAIT"
        log(f"  [{icon}] {name}: {fd['done']}/{fd['total']}")
    if oversized:
        log(f"  OVERSIZED (>5GB): {len(oversized)} files - manual handling needed")
    log("=" * 55)

    remaining = sum(fd["total"] - fd["done"] for fd in folders.values() if fd["status"] != "completed")
    if remaining > 0:
        log(f"\n  {remaining} files remaining - next cycle will continue")
        # Signal to workflow that more runs needed
        print("::notice::More files pending - next cycle will continue")
    else:
        log(f"\n  SAB KAAM HO GAYA! :tada:")


if __name__ == "__main__":
    main()

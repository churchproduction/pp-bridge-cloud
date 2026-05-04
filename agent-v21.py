"""PP Bridge Agent — job runner + control-job runner (second thread)."""
import os, json, time, subprocess, traceback, shutil, threading
import urllib.request, urllib.error, urllib.parse
from urllib.parse import urlparse, parse_qs

# ── Config ─────────────────────────────────────────────────────────
CLOUD_URL    = "https://pp-bridge-cloud.onrender.com"
MACHINE_ID   = "building-c-side-screens"
MACHINE_NAME = "Building C Side Screens"

# ProPresenter API (per-machine — Building C Side Screens)
PP_API_PORT = 1025
PP_API_PASSWORD = "FishHawk"
BRIDGE_SCRIPT = os.path.expanduser("~/pp-bridge/bridge.py")
JOB_WORKDIR  = os.path.expanduser("~/pp-bridge/job-workspace")
HEARTBEAT_EVERY = 30
POLL_EVERY = 3
LOG = os.path.expanduser("~/pp-bridge/logs/agent.log")

# Local ProPresenter API (for the remote)
PP_HOST = "localhost"
PP_PORT = 1025
PP_PASSWORD = "FishHawk"

# Control-job loop tuning
CONTROL_POLL_TIMEOUT = 30        # how long the long-poll GET will hang
CONTROL_BRIDGE_TIMEOUT = 12      # max seconds bridge.py is allowed per control command
CONTROL_RECONNECT_DELAY = 2      # seconds to wait after a connection error

# Remote server
os.makedirs(JOB_WORKDIR, exist_ok=True)

def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}]  {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f: f.write(line + "\n")
    except Exception: pass

# ── Cloud HTTP helpers ─────────────────────────────────────────────
def http(method, path, body=None, timeout=15):
    url = CLOUD_URL + path
    data, headers = None, {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        t = r.read().decode("utf-8")
        return json.loads(t) if t.strip() else None

def download_file(job_id, fname, dest_path, timeout=60):
    url = f"{CLOUD_URL}/api/job/files/{job_id}/{urllib.parse.quote(fname, safe='')}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        with open(dest_path, "wb") as f:
            shutil.copyfileobj(r, f)

# ── Job execution ──────────────────────────────────────────────────
def run_job(job):
    job_id = job["job_id"]
    name = job.get("name", "") or ""
    files = job.get("files", []) or []
    library_adds = job.get("library_adds", []) or []
    log(f"Job {job_id[:8]}: name='{name}', files={len(files)}, library_adds={len(library_adds)}")

    msgs = []
    has_new = bool(files) and bool(name) and not name.startswith("+")
    local_folder = None

    # Step 1: download files and create new presentation
    if has_new:
        local_folder = os.path.join(JOB_WORKDIR, job_id)
        if os.path.exists(local_folder): shutil.rmtree(local_folder)
        os.makedirs(local_folder, exist_ok=True)
        for fname in files:
            try:
                log(f"  Downloading {fname}...")
                download_file(job_id, fname, os.path.join(local_folder, fname))
            except Exception as e:
                return False, f"Download failed for {fname}: {e}"
        log(f"  All files downloaded to {local_folder}")
        try:
            r = subprocess.run(["python3", BRIDGE_SCRIPT, "create", name, local_folder],
                              capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            return False, "bridge.py timed out after 5 min"
        if r.returncode != 0:
            return False, f"bridge.py create failed: {(r.stderr or '').strip()[-400:]}"
        msgs.append(f"Created '{name}' with {len(files)} file(s)")

    # Step 2: add existing library items to Ministries
    added = 0
    failed = []
    for add_name in library_adds:
        try:
            r = subprocess.run(["python3", BRIDGE_SCRIPT, "add_existing", add_name],
                              capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            failed.append(add_name)
            log(f"  add_existing '{add_name}' timed out")
            continue
        if r.returncode == 0:
            added += 1
        else:
            failed.append(add_name)
            log(f"  add_existing '{add_name}' failed: {(r.stderr or '').strip()[-200:]}")
    if added:
        msgs.append(f"Added {added} from library")
    if failed:
        msgs.append(f"{len(failed)} failed")

    # Cleanup
    if local_folder:
        try: shutil.rmtree(local_folder)
        except Exception: pass

    if not msgs:
        return False, "Nothing to do"
    if not has_new and added == 0 and failed:
        return False, "; ".join(msgs)
    return True, "; ".join(msgs)

def cloud_loop():
    log("─" * 60)
    log(f"Agent starting — {MACHINE_ID}")
    log(f"Cloud: {CLOUD_URL}")
    log("─" * 60)
    last_hb, errs = 0, 0
    while True:
        try:
            if time.time() - last_hb > HEARTBEAT_EVERY:
                http("POST", "/api/agent/register",
                     {"machine_id": MACHINE_ID, "name": MACHINE_NAME, "presentations": get_library_presentations()})
                last_hb = time.time()
                if errs > 0: log("Reconnected.")
                errs = 0
            job = http("GET", f"/api/agent/poll/{MACHINE_ID}")
            if job:
                ok, msg = False, ""
                try: ok, msg = run_job(job)
                except Exception as e:
                    msg = f"Exception: {e}"
                log(f"  -> {'OK' if ok else 'FAIL'} {msg}")
                http("POST", f"/api/agent/result/{MACHINE_ID}",
                     {"job_id": job["job_id"], "ok": ok, "message": msg})
            time.sleep(POLL_EVERY)
        except urllib.error.URLError:
            errs += 1
            if errs == 1 or errs % 10 == 0:
                log("Cloud unreachable — retrying...")
            time.sleep(min(30, POLL_EVERY * (1 + errs // 5)))
        except KeyboardInterrupt:
            log("Stopping."); break
        except Exception as e:
            log(f"Error: {e}"); time.sleep(5)

def pp_proxy(method, path, body=None):
    """Forward a request to ProPresenter, transparently adding the password."""
    sep = "&" if "?" in path else "?"
    url = f"http://{PP_HOST}:{PP_PORT}{path}{sep}password={urllib.parse.quote(PP_PASSWORD)}"
    headers = {}
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, r.headers.get("Content-Type", "application/json"), r.read()
    except urllib.error.HTTPError as e:
        return e.code, "application/json", json.dumps({"error": str(e)}).encode()
    except Exception as e:
        return 502, "application/json", json.dumps({"error": str(e)}).encode()


def get_library_presentations():
    """Names of every .pro file across all ProPresenter libraries on disk.
    ProPresenter replaces a presentation when a new one is saved with the
    same filename, so this is the namespace we check for replace warnings."""
    try:
        library_root = os.path.expanduser("~/Documents/ProPresenter/Libraries")
        if not os.path.isdir(library_root):
            return []
        names = []
        for lib in os.listdir(library_root):
            lib_dir = os.path.join(library_root, lib)
            if not os.path.isdir(lib_dir):
                continue
            for fname in os.listdir(lib_dir):
                if fname.endswith(".pro"):
                    names.append(fname[:-4])
        return sorted(set(names))
    except Exception:
        return []


# =============================================================================
# CONTROL JOB LOOP — second thread, long-polls /api/control/poll/<mac>
# =============================================================================

# Whitelist: command -> bridge.py argument template using positional args.
# This keeps the agent dumb — the cloud has already validated arg counts.
CONTROL_COMMANDS = {
    "list_ministries":  lambda a: ["list_ministries"],
    "get_slides":       lambda a: ["get_slides", a[0]],
    "trigger_slide":    lambda a: ["trigger_slide", a[0], a[1]],
    "trigger_next":     lambda a: ["trigger_next"],
    "trigger_previous": lambda a: ["trigger_previous"],
    "clear_slide":      lambda a: ["clear_slide"],
    "delete_from_min":  lambda a: ["delete_from_min", a[0]],
    "reorder_min":      lambda a: ["reorder_min", a[0]],
    "get_thumbnails_bulk": lambda a: ["get_thumbnails_bulk", a[0]],
}

def run_control_job(cmd, args):
    """Dispatch a control command via bridge.py subprocess. Returns (ok, result_obj)."""
    if cmd not in CONTROL_COMMANDS:
        return False, {"ok": False, "error": f"unknown command: {cmd}"}
    try:
        argv = ["python3", BRIDGE_SCRIPT] + CONTROL_COMMANDS[cmd](args)
    except (IndexError, TypeError) as e:
        return False, {"ok": False, "error": f"bad args: {e}"}
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=CONTROL_BRIDGE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, {"ok": False, "error": f"bridge.py timeout ({CONTROL_BRIDGE_TIMEOUT}s)"}
    if r.returncode != 0:
        # bridge.py emits JSON to stdout even on errors for control commands;
        # try to parse it before falling back to stderr text.
        try:
            obj = json.loads((r.stdout or "").strip().splitlines()[-1])
            return False, obj
        except Exception:
            return False, {"ok": False, "error": (r.stderr or r.stdout or "").strip()[-400:]}
    try:
        obj = json.loads((r.stdout or "").strip().splitlines()[-1])
    except Exception as e:
        return False, {"ok": False, "error": f"bad bridge.py output: {e}", "raw": (r.stdout or "")[-400:]}
    return bool(obj.get("ok", True)), obj

def control_loop():
    """Long-polls the cloud for control jobs in a dedicated thread."""
    log(f"Control loop starting (long-poll, {CONTROL_POLL_TIMEOUT}s)")
    errs = 0
    while True:
        try:
            job = http("GET", f"/api/control/poll/{MACHINE_ID}", timeout=CONTROL_POLL_TIMEOUT + 5)
            if errs > 0:
                log("Control loop reconnected.")
                errs = 0
            if not job:
                # Long-poll returned with no job (timeout). Reconnect immediately.
                continue
            jid = job.get("job_id")
            cmd = job.get("command", "")
            args = job.get("args", []) or []
            log(f"Ctrl {jid[:8]}: {cmd}({', '.join(repr(a)[:30] for a in args)})")
            ok, result = run_control_job(cmd, args)
            try:
                http("POST", f"/api/control/result/{MACHINE_ID}",
                     {"job_id": jid, "ok": ok, "result": result})
            except Exception as e:
                log(f"  failed to post control result: {e}")
        except urllib.error.URLError:
            errs += 1
            if errs == 1 or errs % 10 == 0:
                log("Control loop: cloud unreachable — retrying...")
            time.sleep(CONTROL_RECONNECT_DELAY)
        except KeyboardInterrupt:
            log("Control loop stopping.")
            break
        except Exception as e:
            log(f"Control loop error: {e}")
            time.sleep(CONTROL_RECONNECT_DELAY)


if __name__ == "__main__":
    # Start control loop in a daemon thread so the upload loop stays in main thread
    # and Ctrl-C / process kill behaves the same as before.
    t = threading.Thread(target=control_loop, daemon=True, name="control-loop")
    t.start()
    cloud_loop()

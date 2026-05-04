"""ProPresenter Bridge — Production Cloud Server (with PowerPoint conversion + remote control)."""
import json, os, time, uuid, threading, sqlite3, logging, re, unicodedata, subprocess, shutil, tempfile, glob
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

PORT = int(os.environ.get("PORT", "8787"))
DB_PATH = os.environ.get("DB_PATH", "/tmp/ppbridge.db")
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/tmp/ppbridge-uploads")
HEARTBEAT_TIMEOUT = 90
MAX_UPLOAD = 200 * 1024 * 1024

# Control-job tuning
AGENT_LONGPOLL_SECONDS = 25     # how long the agent's GET hangs waiting for a control job
CONTROL_RESULT_TIMEOUT = 15     # how long the frontend POST waits for the agent to finish

os.makedirs(UPLOAD_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("cloud")

def sanitize_filename(name):
    """Strip unicode oddities (narrow space, smart quotes, emoji), unsafe chars."""
    name = unicodedata.normalize("NFKD", name)
    name = name.encode("ascii", "ignore").decode("ascii")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name)
    name = name.strip(". ")
    return name or "file"

def convert_pptx_to_pngs(pptx_path, output_dir):
    """Convert a .pptx to a list of PNGs using LibreOffice + pdftoppm."""
    workdir = tempfile.mkdtemp(prefix="ppconv-")
    try:
        log.info(f"Converting {pptx_path} via LibreOffice...")
        result = subprocess.run([
            "libreoffice", "--headless", "--convert-to", "pdf",
            "--outdir", workdir, pptx_path
        ], capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            log.error(f"LibreOffice failed: {result.stderr[:500]}")
            raise RuntimeError(f"LibreOffice conversion failed: {result.stderr[:300]}")
        pdfs = glob.glob(os.path.join(workdir, "*.pdf"))
        if not pdfs:
            raise RuntimeError("LibreOffice produced no PDF")
        pdf_path = pdfs[0]
        log.info(f"Got PDF: {pdf_path}, rendering pages...")
        base = os.path.join(output_dir, "slide")
        result = subprocess.run([
            "pdftoppm", "-png", "-scale-to-x", "1920", "-scale-to-y", "-1",
            pdf_path, base
        ], capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            log.error(f"pdftoppm failed: {result.stderr[:500]}")
            raise RuntimeError(f"PDF render failed: {result.stderr[:300]}")
        pngs = sorted([os.path.basename(p) for p in glob.glob(os.path.join(output_dir, "slide*.png"))])
        log.info(f"Produced {len(pngs)} PNG slides")
        return pngs
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

db_lock = threading.Lock()

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    with db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS agents (
                machine_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                last_seen REAL NOT NULL,
                presentations TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                machine_id TEXT NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                folder TEXT NOT NULL,
                files_json TEXT NOT NULL,
                library_adds_json TEXT NOT NULL DEFAULT '[]',
                result TEXT DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_machine_status ON jobs(machine_id, status);
            CREATE TABLE IF NOT EXISTS control_jobs (
                job_id TEXT PRIMARY KEY,
                machine_id TEXT NOT NULL,
                command TEXT NOT NULL,
                args_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL,
                result_json TEXT DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_control_machine_status ON control_jobs(machine_id, status);
            CREATE INDEX IF NOT EXISTS idx_control_created ON control_jobs(created_at);
        """)
        for ddl in [
            "ALTER TABLE agents ADD COLUMN presentations TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE jobs ADD COLUMN library_adds_json TEXT NOT NULL DEFAULT '[]'",
        ]:
            try:
                c.execute(ddl)
            except sqlite3.OperationalError:
                pass
init_db()

def now(): return time.time()
def is_online_row(r): return r and (now() - r["last_seen"]) < HEARTBEAT_TIMEOUT

# =============================================================================
# Periodic cleanup — remove old finished/abandoned control jobs so the table
# doesn't grow indefinitely. Runs in a background thread.
# =============================================================================
def control_cleanup_loop():
    while True:
        try:
            with db_lock, db() as c:
                cutoff_done = now() - 600        # 10 min for completed
                cutoff_orphan = now() - 120      # 2 min for stuck dispatched/queued
                c.execute("DELETE FROM control_jobs WHERE status IN ('done','failed') AND updated_at < ?",
                          (cutoff_done,))
                c.execute("UPDATE control_jobs SET status='failed', result_json=?, updated_at=? "
                          "WHERE status IN ('queued','dispatched') AND created_at < ?",
                          (json.dumps({"ok": False, "error": "agent did not respond in time"}),
                           now(), cutoff_orphan))
                c.commit()
        except Exception as e:
            log.warning(f"control cleanup error: {e}")
        time.sleep(60)

threading.Thread(target=control_cleanup_loop, daemon=True).start()

class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Skip noisy long-poll log lines
        if "/api/control/poll/" in self.path and args and args[1] == "200":
            return
        log.info(f"{self.command} {self.path} -> {args[1]}")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code); self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def _send_text(self, code, text):
        body = text.encode()
        self.send_response(code); self._cors()
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def _send_html(self, code, html):
        body = html.encode()
        self.send_response(code); self._cors()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode()) if n else {}

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            return self._send_text(200, "PP Bridge cloud — alive")
        if u.path == "/upload":
            return self._upload_form()
        if u.path == "/api/status":
            with db_lock, db() as c:
                rows = c.execute("SELECT * FROM agents").fetchall()
                agents = []
                for r in rows:
                    q = c.execute("SELECT COUNT(*) FROM jobs WHERE machine_id=? AND status='queued'",
                                  (r["machine_id"],)).fetchone()[0]
                    try:
                        pres = json.loads(r["presentations"] or "[]")
                        if not isinstance(pres, list): pres = []
                    except Exception:
                        pres = []
                    agents.append({"machine_id": r["machine_id"], "name": r["name"],
                                   "online": is_online_row(r), "queued": q,
                                   "last_seen_ago": round(now() - r["last_seen"], 1),
                                   "presentations": pres})
                jobs = [{"job_id": j["job_id"], "machine_id": j["machine_id"],
                         "name": j["name"], "status": j["status"], "result": j["result"]}
                        for j in c.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 30")]
            return self._send_json(200, {"agents": agents, "jobs": jobs})
        if u.path.startswith("/api/agent/poll/"):
            mid = u.path.rsplit("/", 1)[-1]
            with db_lock, db() as c:
                if not c.execute("SELECT 1 FROM agents WHERE machine_id=?", (mid,)).fetchone():
                    return self._send_json(404, {"error": "unknown agent"})
                c.execute("UPDATE agents SET last_seen=? WHERE machine_id=?", (now(), mid))
                row = c.execute("SELECT * FROM jobs WHERE machine_id=? AND status='queued' "
                                "ORDER BY created_at LIMIT 1", (mid,)).fetchone()
                if not row:
                    c.commit(); return self._send_json(200, None)
                c.execute("UPDATE jobs SET status='dispatched', updated_at=? WHERE job_id=?",
                          (now(), row["job_id"]))
                c.commit()
                try:
                    library_adds = json.loads(row["library_adds_json"] or "[]")
                    if not isinstance(library_adds, list): library_adds = []
                except Exception:
                    library_adds = []
                job = {"job_id": row["job_id"], "machine_id": row["machine_id"],
                       "name": row["name"], "folder": row["folder"],
                       "files": json.loads(row["files_json"]),
                       "library_adds": library_adds}
            return self._send_json(200, job)
        if u.path.startswith("/api/control/poll/"):
            return self._control_poll(u.path.rsplit("/", 1)[-1])
        if u.path.startswith("/api/job/files/"):
            parts = u.path.split("/")
            if len(parts) < 6: return self._send_json(400, {"error": "bad path"})
            jid, fname = parts[-2], parts[-1]
            fname = unquote(fname)
            path = os.path.join(UPLOAD_DIR, jid, fname)
            if not os.path.exists(path): return self._send_json(404, {"error": "not found"})
            with open(path, "rb") as f: data = f.read()
            self.send_response(200); self._cors()
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data); return
        return self._send_json(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/agent/register":
            b = self._read_json()
            mid, name = b.get("machine_id"), b.get("name", "Unknown")
            if not mid: return self._send_json(400, {"error": "missing machine_id"})
            pres = b.get("presentations", [])
            if not isinstance(pres, list): pres = []
            pres = [str(p)[:200] for p in pres[:200]]
            pres_json = json.dumps(pres)
            with db_lock, db() as c:
                if c.execute("SELECT 1 FROM agents WHERE machine_id=?", (mid,)).fetchone():
                    c.execute("UPDATE agents SET name=?, last_seen=?, presentations=? WHERE machine_id=?",
                              (name, now(), pres_json, mid))
                else:
                    log.info(f"  -> New agent: {name} ({mid})")
                    c.execute("INSERT INTO agents(machine_id, name, last_seen, presentations) VALUES(?,?,?,?)",
                              (mid, name, now(), pres_json))
                c.commit()
            return self._send_json(200, {"ok": True})
        if u.path.startswith("/api/agent/result/"):
            b = self._read_json()
            jid = b.get("job_id"); ok = bool(b.get("ok")); msg = b.get("message", "")[:1000]
            with db_lock, db() as c:
                c.execute("UPDATE jobs SET status=?, result=?, updated_at=? WHERE job_id=?",
                          ("done" if ok else "failed", msg, now(), jid))
                c.commit()
            log.info(f"  -> Job {jid[:8]} {'done' if ok else 'failed'}: {msg}")
            return self._send_json(200, {"ok": True})
        if u.path.startswith("/api/control/result/"):
            return self._control_result()
        if u.path == "/api/control":
            return self._control_submit()
        if u.path == "/api/jobs":
            return self._submit_job()
        return self._send_json(404, {"error": "not found"})

    # =========================================================================
    # CONTROL JOBS — synchronous remote-control over the agent's bridge.py
    # =========================================================================
    # Allowed commands the frontend can send. Anything not in this list is rejected.
    CONTROL_COMMANDS = {
        # read-only
        "list_ministries":   {"args": 0, "max_wait": 10},
        "get_slides":        {"args": 1, "max_wait": 10},
        # mutations
        "delete_from_min":   {"args": 1, "max_wait": 12},
        "reorder_min":       {"args": 1, "max_wait": 12},
        # triggers (fast — short timeout)
        "trigger_slide":     {"args": 2, "max_wait": 6},
        "trigger_next":      {"args": 0, "max_wait": 6},
        "trigger_previous":  {"args": 0, "max_wait": 6},
        "clear_slide":       {"args": 0, "max_wait": 6},
    }

    def _control_submit(self):
        """Frontend submits a control job and waits synchronously for the result."""
        b = self._read_json()
        mid = b.get("machine_id")
        cmd = b.get("command")
        args = b.get("args", [])
        if not mid or not cmd:
            return self._send_json(400, {"error": "need machine_id and command"})
        if cmd not in self.CONTROL_COMMANDS:
            return self._send_json(400, {"error": f"unknown command: {cmd}"})
        spec = self.CONTROL_COMMANDS[cmd]
        if not isinstance(args, list):
            return self._send_json(400, {"error": "args must be a list"})
        if len(args) != spec["args"]:
            return self._send_json(400, {"error": f"{cmd} expects {spec['args']} arg(s), got {len(args)}"})
        args = [str(a)[:1000] for a in args]
        # Verify the agent exists
        with db_lock, db() as c:
            ag = c.execute("SELECT * FROM agents WHERE machine_id=?", (mid,)).fetchone()
        if not ag:
            return self._send_json(404, {"error": "unknown machine_id"})
        if not is_online_row(ag):
            return self._send_json(503, {"error": "machine offline"})
        jid = uuid.uuid4().hex
        ts = now()
        with db_lock, db() as c:
            c.execute("INSERT INTO control_jobs(job_id, machine_id, command, args_json, status, "
                      "result_json, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                      (jid, mid, cmd, json.dumps(args), "queued", "", ts, ts))
            c.commit()
        # Wait up to spec["max_wait"] for the agent to complete the job
        deadline = time.time() + min(spec["max_wait"], CONTROL_RESULT_TIMEOUT)
        while time.time() < deadline:
            with db_lock, db() as c:
                row = c.execute("SELECT status, result_json FROM control_jobs WHERE job_id=?",
                                (jid,)).fetchone()
            if row and row["status"] in ("done", "failed"):
                try:
                    result = json.loads(row["result_json"]) if row["result_json"] else None
                except Exception:
                    result = {"ok": False, "error": "malformed result"}
                return self._send_json(200, {
                    "job_id": jid,
                    "ok": row["status"] == "done" and (result.get("ok", True) if isinstance(result, dict) else True),
                    "result": result,
                })
            time.sleep(0.1)
        return self._send_json(504, {"job_id": jid, "ok": False,
                                     "error": f"timed out after {spec['max_wait']}s"})

    def _control_poll(self, mid):
        """Agent's long-poll endpoint. Hangs up to AGENT_LONGPOLL_SECONDS waiting for a queued control job."""
        # Verify the agent
        with db_lock, db() as c:
            if not c.execute("SELECT 1 FROM agents WHERE machine_id=?", (mid,)).fetchone():
                return self._send_json(404, {"error": "unknown agent"})
            c.execute("UPDATE agents SET last_seen=? WHERE machine_id=?", (now(), mid))
            c.commit()
        deadline = time.time() + AGENT_LONGPOLL_SECONDS
        while time.time() < deadline:
            with db_lock, db() as c:
                row = c.execute("SELECT * FROM control_jobs WHERE machine_id=? AND status='queued' "
                                "ORDER BY created_at LIMIT 1", (mid,)).fetchone()
                if row:
                    c.execute("UPDATE control_jobs SET status='dispatched', updated_at=? WHERE job_id=?",
                              (now(), row["job_id"]))
                    c.commit()
                    try:
                        args = json.loads(row["args_json"] or "[]")
                    except Exception:
                        args = []
                    return self._send_json(200, {
                        "job_id": row["job_id"],
                        "command": row["command"],
                        "args": args,
                    })
            time.sleep(0.15)
        return self._send_json(200, None)

    def _control_result(self):
        """Agent reports a control-job result back to the cloud."""
        b = self._read_json()
        jid = b.get("job_id")
        ok = bool(b.get("ok"))
        result = b.get("result")
        if not jid:
            return self._send_json(400, {"error": "missing job_id"})
        try:
            rj = json.dumps(result) if result is not None else ""
        except Exception:
            rj = json.dumps({"ok": False, "error": "could not serialize result"})
        with db_lock, db() as c:
            c.execute("UPDATE control_jobs SET status=?, result_json=?, updated_at=? WHERE job_id=?",
                      ("done" if ok else "failed", rj, now(), jid))
            c.commit()
        return self._send_json(200, {"ok": True})

    def _upload_form(self):
        agents_json = "[]"
        try:
            with db_lock, db() as c:
                rows = c.execute("SELECT machine_id, name, last_seen FROM agents").fetchall()
                agents_json = json.dumps([
                    {"id": r["machine_id"], "name": r["name"],
                     "online": is_online_row(r)}
                    for r in rows
                ])
        except Exception:
            pass
        html = r"""<!doctype html><meta charset=utf-8><title>Upload</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box}
body{font:16px/1.5 -apple-system,system-ui,sans-serif;background:#0f0d0a;color:#f4f4f0;padding:24px;max-width:520px;margin:0 auto;min-height:100vh}
h1{font-weight:300;font-size:32px;margin:8px 0 4px}
.sub{color:#8f8d86;font-size:14px;margin-bottom:32px}
label{display:block;color:#8f8d86;font-size:11px;letter-spacing:.08em;text-transform:uppercase;margin-top:20px;margin-bottom:6px}
input,select,button{font:inherit;background:#15120e;color:#f4f4f0;border:1px solid #2a2620;border-radius:8px;padding:14px;width:100%}
input[type=file]{padding:12px}
button{background:#dcc78a;color:#0f0d0a;border:0;font-weight:500;padding:16px;margin-top:28px;cursor:pointer;font-size:16px}
button:disabled{opacity:.5}
.status{margin-top:24px;padding:14px;border-radius:8px;display:none}
.status.show{display:block}
.ok{background:rgba(134,239,172,.1);color:#86efac;border:1px solid rgba(134,239,172,.3)}
.err{background:rgba(248,113,113,.1);color:#f87171;border:1px solid rgba(248,113,113,.3)}
</style>
<h1>Upload to ProPresenter</h1>
<div class=sub>Submits to the Ministries playlist. Images, videos, or .pptx all work.</div>
<form id=f>
  <label>Presentation name</label>
  <input name=name placeholder="Sunday Announcements" required>
  <label>Send to</label>
  <select name=machine_id required id=machineSelect></select>
  <label>Files (images / videos / .pptx)</label>
  <input type=file name=files multiple required accept="image/*,video/*,.pptx">
  <button type=submit id=submitBtn>Submit</button>
</form>
<div id=status class=status></div>
<script>
const agents = __AGENTS__;
const sel = document.getElementById('machineSelect');
agents.forEach(a => {
  const o = document.createElement('option');
  o.value = a.id;
  o.textContent = (a.online ? '\u{1F7E2} ' : '\u26AA ') + a.name + (a.online ? '' : ' (offline -- will queue)');
  sel.appendChild(o);
});
if (!agents.length) {
  const o = document.createElement('option');
  o.disabled = true; o.textContent = 'No machines registered';
  sel.appendChild(o);
}
const f = document.getElementById('f'), btn = document.getElementById('submitBtn'), st = document.getElementById('status');
f.addEventListener('submit', async e => {
  e.preventDefault();
  btn.disabled = true; btn.textContent = 'Submitting...';
  st.className = 'status'; st.textContent = '';
  const fd = new FormData(f);
  try {
    const r = await fetch('/api/jobs', { method: 'POST', body: fd });
    const j = await r.json();
    if (r.ok) {
      st.className = 'status show ok';
      st.textContent = "Submitted. Job " + j.job_id.slice(0,8) + " -- watch ProPresenter.";
      f.reset();
    } else {
      st.className = 'status show err';
      st.textContent = 'Error: ' + (j.error || 'unknown');
    }
  } catch (err) {
    st.className = 'status show err';
    st.textContent = 'Network error: ' + err;
  } finally {
    btn.disabled = false; btn.textContent = 'Submit';
  }
});
</script>"""
        html = html.replace("__AGENTS__", agents_json)
        return self._send_html(200, html)

    def _submit_job(self):
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            return self._send_json(400, {"error": "expected multipart/form-data"})
        length = int(self.headers["Content-Length"])
        if length > MAX_UPLOAD: return self._send_json(413, {"error": "too large"})
        boundary = ctype.split("boundary=", 1)[1].encode()
        raw = self.rfile.read(length)
        parts = raw.split(b"--" + boundary)
        machine_id = name = None
        library_adds_raw = ""
        files = []
        for p in parts:
            p = p.strip(b"\r\n")
            if not p or p == b"--" or b"\r\n\r\n" not in p: continue
            head, _, content = p.partition(b"\r\n\r\n")
            content = content.rstrip(b"\r\n--")
            head_text = head.decode("utf-8", "replace")
            disp = [l for l in head_text.split("\r\n") if l.lower().startswith("content-disposition")]
            if not disp: continue
            fields = dict(s.strip().split("=", 1) for s in disp[0].split(";")[1:] if "=" in s)
            field_name = fields.get("name", "").strip('"')
            filename = fields.get("filename", "").strip('"') if "filename" in fields else None
            if filename:
                files.append((filename, content))
            elif field_name == "machine_id":
                machine_id = content.decode()
            elif field_name == "name":
                name = content.decode()
            elif field_name == "library_adds":
                library_adds_raw = content.decode()
        library_adds = []
        if library_adds_raw:
            try:
                parsed = json.loads(library_adds_raw)
                if isinstance(parsed, list):
                    library_adds = [str(x)[:200] for x in parsed[:50] if x]
            except Exception:
                pass
        if not machine_id:
            return self._send_json(400, {"error": "need machine_id"})
        has_new = bool(name and files)
        has_existing = bool(library_adds)
        if not has_new and not has_existing:
            return self._send_json(400, {"error": "need either (name + files) or library_adds"})
        jid = str(uuid.uuid4())
        jdir = os.path.join(UPLOAD_DIR, jid)
        os.makedirs(jdir, exist_ok=True)
        saved = []
        pptx_paths = []
        for fname, data in files:
            safe = sanitize_filename(os.path.basename(fname))
            base, ext = os.path.splitext(safe)
            n, final = 1, safe
            while os.path.exists(os.path.join(jdir, final)):
                final = f"{base}-{n}{ext}"
                n += 1
            full = os.path.join(jdir, final)
            with open(full, "wb") as f:
                f.write(data)
            if final.lower().endswith(".pptx"):
                pptx_paths.append(full)
            else:
                saved.append(final)
        for pptx in pptx_paths:
            try:
                pngs = convert_pptx_to_pngs(pptx, jdir)
                saved.extend(pngs)
                try: os.remove(pptx)
                except Exception: pass
            except Exception as e:
                log.error(f"PPTX conversion failed for {pptx}: {e}")
                return self._send_json(500, {"error": f"PPTX conversion failed: {e}"})
        if has_new and not saved:
            return self._send_json(400, {"error": "no usable files after processing"})
        display_name = name if name else f"+{len(library_adds)} from library"
        ts = now()
        with db_lock, db() as c:
            if not c.execute("SELECT 1 FROM agents WHERE machine_id=?", (machine_id,)).fetchone():
                c.execute("INSERT INTO agents(machine_id, name, last_seen, presentations) VALUES(?,?,0,'[]')",
                          (machine_id, "(offline)"))
            c.execute("INSERT INTO jobs(job_id, machine_id, name, status, folder, files_json, "
                      "library_adds_json, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                      (jid, machine_id, display_name, "queued", jdir,
                       json.dumps(saved), json.dumps(library_adds), ts, ts))
            c.commit()
        log.info(f"  -> Job {jid[:8]} queued for {machine_id}: '{display_name}' "
                 f"({len(saved)} files, {len(library_adds)} library_adds)")
        return self._send_json(200, {"job_id": jid, "status": "queued"})


if __name__ == "__main__":
    log.info(f"PP Bridge cloud listening on :{PORT}")
    log.info(f"DB: {DB_PATH}")
    log.info(f"Uploads: {UPLOAD_DIR}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()

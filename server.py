"""ProPresenter Bridge — Production Cloud Server."""
import json, os, time, uuid, threading, sqlite3, logging
import re, unicodedata
def sanitize_filename(name):
    # ASCII-fold unicode (strips narrow space \u202f, smart quotes, emojis, etc.)
    name = unicodedata.normalize("NFKD", name)
    name = name.encode("ascii", "ignore").decode("ascii")
    # Strip any remaining unsafe path chars
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name)
    name = name.strip(". ")
    return name or "file"
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT       = int(os.environ.get("PORT", "8787"))
DB_PATH    = os.environ.get("DB_PATH", "/tmp/ppbridge.db")
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/tmp/ppbridge-uploads")
HEARTBEAT_TIMEOUT = 90
MAX_UPLOAD = 200 * 1024 * 1024

os.makedirs(UPLOAD_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("cloud")

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
            name       TEXT NOT NULL,
            last_seen  REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS jobs (
            job_id      TEXT PRIMARY KEY,
            machine_id  TEXT NOT NULL,
            name        TEXT NOT NULL,
            status      TEXT NOT NULL,
            folder      TEXT NOT NULL,
            files_json  TEXT NOT NULL,
            result      TEXT DEFAULT '',
            created_at  REAL NOT NULL,
            updated_at  REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_machine_status ON jobs(machine_id, status);
        """)
init_db()

def now(): return time.time()
def is_online_row(r): return r and (now() - r["last_seen"]) < HEARTBEAT_TIMEOUT

class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info(f"{self.command} {self.path}  ->  {args[1]}")

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
                    agents.append({"machine_id": r["machine_id"], "name": r["name"],
                                   "online": is_online_row(r), "queued": q,
                                   "last_seen_ago": round(now() - r["last_seen"], 1)})
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
                job = {"job_id": row["job_id"], "machine_id": row["machine_id"],
                       "name": row["name"], "folder": row["folder"],
                       "files": json.loads(row["files_json"])}
            return self._send_json(200, job)
        if u.path.startswith("/api/job/files/"):
            parts = u.path.split("/")
            if len(parts) < 6: return self._send_json(400, {"error": "bad path"})
            jid, fname = parts[-2], parts[-1]
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
            with db_lock, db() as c:
                if c.execute("SELECT 1 FROM agents WHERE machine_id=?", (mid,)).fetchone():
                    c.execute("UPDATE agents SET name=?, last_seen=? WHERE machine_id=?",
                              (name, now(), mid))
                else:
                    log.info(f"  -> New agent: {name} ({mid})")
                    c.execute("INSERT INTO agents(machine_id, name, last_seen) VALUES(?,?,?)",
                              (mid, name, now()))
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
        if u.path == "/api/jobs": return self._submit_job()
        return self._send_json(404, {"error": "not found"})

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
<div class=sub>Submits to the Ministries playlist on the chosen machine.</div>
<form id=f>
<label>Presentation name</label>
<input name=name placeholder="Sunday Announcements" required>
<label>Send to</label>
<select name=machine_id required id=machineSelect></select>
<label>Files (images / videos)</label>
<input type=file name=files multiple required accept="image/*,video/*">
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
        machine_id = name = None; files = []
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
            if filename: files.append((filename, content))
            elif field_name == "machine_id": machine_id = content.decode()
            elif field_name == "name": name = content.decode()
        if not machine_id or not name or not files:
            return self._send_json(400, {"error": "need machine_id, name, files"})
        jid = str(uuid.uuid4())
        jdir = os.path.join(UPLOAD_DIR, jid); os.makedirs(jdir, exist_ok=True)
        saved = []
        for fname, data in files:
            safe = sanitize_filename(os.path.basename(fname))
            base, ext = os.path.splitext(safe)
            n, final = 1, safe
            while os.path.exists(os.path.join(jdir, final)):
                final = f"{base}-{n}{ext}"; n += 1
            with open(os.path.join(jdir, final), "wb") as f: f.write(data)
            saved.append(final)
        ts = now()
        with db_lock, db() as c:
            if not c.execute("SELECT 1 FROM agents WHERE machine_id=?", (machine_id,)).fetchone():
                c.execute("INSERT INTO agents(machine_id, name, last_seen) VALUES(?,?,0)",
                          (machine_id, "(offline)"))
            c.execute("INSERT INTO jobs(job_id, machine_id, name, status, folder, files_json, "
                      "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                      (jid, machine_id, name, "queued", jdir, json.dumps(saved), ts, ts))
            c.commit()
        log.info(f"  -> Job {jid[:8]} queued for {machine_id}: '{name}' ({len(saved)} files)")
        return self._send_json(200, {"job_id": jid, "status": "queued"})

if __name__ == "__main__":
    log.info(f"PP Bridge cloud listening on :{PORT}")
    log.info(f"DB: {DB_PATH}")
    log.info(f"Uploads: {UPLOAD_DIR}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()

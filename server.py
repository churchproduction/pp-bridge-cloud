"""ProPresenter Bridge — Production Cloud Server."""
import json, os, time, uuid, threading, sqlite3, logging
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

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode()) if n else {}

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            return self._send_text(200, "PP Bridge cloud — alive")
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
            with open(os.path.join(jdir, os.path.basename(fname)), "wb") as f: f.write(data)
            saved.append(os.path.basename(fname))
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

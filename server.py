"""ProPresenter Bridge — Production Cloud Server (with PowerPoint conversion + remote control)."""
import json, os, time, uuid, threading, sqlite3, logging, re, unicodedata, subprocess, shutil, tempfile, glob
from datetime import datetime
try:
    from zoneinfo import ZoneInfo  # Python 3.9+ — used for service-time lockout
except ImportError:
    ZoneInfo = None
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

# =============================================================================
# PLAYLIST_CONFIG — per-Mac whitelist of playlists the frontend is allowed to
# see + control + upload to. Anything not listed here is invisible to users.
# Locked playlists require the matching frontend password (handled in JS).
# =============================================================================
# Lock keys (pure labels — passwords live in the frontend JS, hashed):
#   "educator"  → existing Educator123! gate (Building C)
#   "kids"      → FCKids
#   "students"  → StudentsFC
PLAYLIST_CONFIG = {
    "mac-mini-production": {
        "label": "Students",
        "playlists": [
            {"uuid": "1E89A841-7ED9-4A25-B1E8-89EAEC855827",
             "name": "Ministries", "locked": False},
            {"uuid": "F5B3B656-DF01-4EB8-A1A3-BBBB2E221C69",
             "name": "Students", "locked": True, "lock_key": "students"},
        ],
    },
    "kids-downstairs": {
        "label": "Kids Downstairs",
        "playlists": [
            {"uuid": "8C742596-1DD9-467D-A675-3060B94E19B0",
             "name": "Ministries", "locked": False},
            {"uuid": "FA3A4576-C0BE-4E5C-826B-B9DC9881AD48",
             "name": "Kids", "locked": True, "lock_key": "kids"},
        ],
    },
    "building-c-led-wall": {
        "label": "Building C LED Wall",
        "playlists": [
            {"uuid": "0C93437E-BC83-428E-8F8C-289CF9AA049E",
             "name": "Ministries", "locked": False},
            {"uuid": "A42AF03D-1393-4D21-860E-8AB83F24F579",
             "name": "Sunday Mornings", "locked": True, "lock_key": "educator"},
        ],
    },
    "building-c-side-screens": {
        "label": "Building C Side Screens",
        "playlists": [
            {"uuid": "11221733-3866-44D9-9CDC-6FCA837691C1",
             "name": "Ministries", "locked": False},
            {"uuid": "402B033F-F602-42B1-992D-9F85B1DD41F8",
             "name": "Sunday Mornings", "locked": True, "lock_key": "educator"},
        ],
    },
}

def get_playlists_for_machine(machine_id):
    """Return the configured playlists for a machine, or [] if not in config."""
    cfg = PLAYLIST_CONFIG.get(machine_id)
    if not cfg:
        return []
    return cfg.get("playlists", [])

def is_playlist_allowed(machine_id, playlist_uuid):
    """True if the given playlist UUID is in the whitelist for this machine.
    Used to reject mutations against unknown playlists from the cloud side."""
    if not playlist_uuid:
        return False
    for pl in get_playlists_for_machine(machine_id):
        if pl.get("uuid", "").lower() == playlist_uuid.lower():
            return True
    return False


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
                playlist_uuid TEXT NOT NULL DEFAULT '',
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
            CREATE TABLE IF NOT EXISTS meta (
                k TEXT PRIMARY KEY,
                v TEXT NOT NULL DEFAULT ''
            );
        """)
        for ddl in [
            "ALTER TABLE agents ADD COLUMN presentations TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE jobs ADD COLUMN library_adds_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE jobs ADD COLUMN playlist_uuid TEXT NOT NULL DEFAULT ''",
        ]:
            try:
                c.execute(ddl)
            except sqlite3.OperationalError:
                pass
init_db()

def now(): return time.time()

# ─── Service-time lockout ─────────────────────────────────────────────
# Blocks /api/control and /api/jobs from accepting user-initiated requests
# during Sunday morning service hours, unless the request includes a valid
# X-Lockout-Override header.
#
# To adjust: edit these constants and redeploy. To disable entirely, set
# LOCKOUT_DAY = -1.
LOCKOUT_DAY = 6                  # Monday=0, ..., Sunday=6.  Use -1 to disable lockout.
LOCKOUT_START_HOUR = 7           # 7 AM Eastern (24-hour clock)
LOCKOUT_END_HOUR = 13            # 1 PM Eastern (exclusive — 12:59 locked, 13:00 open)
LOCKOUT_TZ = "America/New_York"  # zoneinfo handles DST automatically
# SHA256(salt + "Educator123!") where salt = "pp-bridge-control-2026"
LOCKOUT_OVERRIDE_HASH = "0b50f7b941b65e01ee453f57a2e7557837034342005bdcb344a6e446f4af27ec"

def is_locked_now():
    """True if we are inside a Sunday service lockout window."""
    if LOCKOUT_DAY < 0 or ZoneInfo is None:
        return False
    try:
        n = datetime.now(ZoneInfo(LOCKOUT_TZ))
    except Exception:
        return False
    return n.weekday() == LOCKOUT_DAY and LOCKOUT_START_HOUR <= n.hour < LOCKOUT_END_HOUR

def _has_lockout_override(headers):
    return (headers.get("X-Lockout-Override") or "").strip().lower() == LOCKOUT_OVERRIDE_HASH.lower()

def is_request_blocked(headers):
    """True if we should reject this request because of the lockout or kill switch."""
    # Kill switch (admin emergency lockdown) trumps everything — no override works
    if is_killswitch_active():
        return True
    return is_locked_now() and not _has_lockout_override(headers)

# ──────────────────────────────────────────────────────────────────────
# Emergency kill switch — admin-triggered lockdown that blocks ALL
# uploads + remote actions. Activated by hitting a secret URL with the
# correct token. State persists in the meta table so it survives restarts.
# ──────────────────────────────────────────────────────────────────────
KILL_SWITCH_TOKEN = (os.environ.get("KILL_SWITCH_TOKEN") or "").strip()

def is_killswitch_active():
    """True if the admin emergency lockdown is currently engaged."""
    try:
        with db_lock, db() as c:
            r = c.execute("SELECT v FROM meta WHERE k='killswitch'").fetchone()
        return bool(r and r["v"] == "1")
    except Exception:
        # If the meta table doesn't exist yet (fresh install), default to safe-open
        return False

def set_killswitch(active):
    with db_lock, db() as c:
        c.execute("INSERT INTO meta(k, v) VALUES('killswitch', ?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                  ("1" if active else "0",))
        c.commit()

# ──────────────────────────────────────────────────────────────────────
# Discord webhook notifications
# Webhook URLs come from environment variables on Render — never put them
# in code or commit them. Set in Render Dashboard → Environment.
# ──────────────────────────────────────────────────────────────────────
import urllib.request as _urlreq

DISCORD_WEBHOOKS = {
    "uploads": (os.environ.get("DISCORD_UPLOADS_URL") or "").strip(),
    "remote":  (os.environ.get("DISCORD_REMOTE_URL")  or "").strip(),
    "gate":    (os.environ.get("DISCORD_GATE_URL")    or "").strip(),
}

# Discord embed colors (decimal RGB)
COLOR_GREEN  = 5763719    # success
COLOR_RED    = 15548997   # failure
COLOR_BLUE   = 5793266    # info / control action
COLOR_GRAY   = 9807270    # neutral / queued
COLOR_ORANGE = 15105570   # warning / mutation

def discord_post(channel, embed):
    """Post a single embed to the named Discord channel webhook.
    Non-blocking — fires off in a background thread, ignores failures."""
    url = DISCORD_WEBHOOKS.get(channel)
    if not url:
        return
    def _send():
        try:
            payload = {"embeds": [embed]}
            req = _urlreq.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    # Discord blocks Python-urllib's default User-Agent.
                    # Any meaningful UA string is accepted.
                    "User-Agent": "PP-Bridge-Cloud (https://github.com/churchproduction/pp-bridge-cloud)",
                },
                method="POST",
            )
            _urlreq.urlopen(req, timeout=5)
        except Exception as e:
            log.warning(f"Discord post to {channel} failed: {e}")
    threading.Thread(target=_send, daemon=True).start()

def client_ip(handler):
    """Best-effort client IP extraction. Render's reverse proxy uses X-Forwarded-For."""
    fwd = handler.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    cf = handler.headers.get("CF-Connecting-IP", "")
    if cf:
        return cf.strip()
    if handler.client_address:
        return handler.client_address[0]
    return "unknown"

def short_ua(ua, n=80):
    """Trim a User-Agent string to its meaningful prefix."""
    if not ua: return ""
    return (ua[:n] + "…") if len(ua) > n else ua

# ──────────────────────────────────────────────────────────────────────
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

# Agent online/offline watcher — posts to Discord #remote when an agent's
# status changes. Runs every 30 seconds.
_agent_watch_state = {}
def agent_watch_loop():
    global _agent_watch_state
    # Seed initial state silently so we don't spam at startup
    try:
        with db_lock, db() as c:
            for r in c.execute("SELECT machine_id, name, last_seen FROM agents").fetchall():
                _agent_watch_state[r["machine_id"]] = (r["name"], is_online_row(r))
    except Exception:
        pass
    while True:
        try:
            with db_lock, db() as c:
                rows = c.execute("SELECT machine_id, name, last_seen FROM agents").fetchall()
            for r in rows:
                mid = r["machine_id"]
                name = r["name"]
                online = is_online_row(r)
                prev = _agent_watch_state.get(mid)
                if prev is None:
                    _agent_watch_state[mid] = (name, online)
                    continue
                if prev[1] != online:
                    discord_post("remote", {
                        "title": (f"🟢 {name} came online" if online else f"🔴 {name} went offline"),
                        "color": COLOR_GREEN if online else COLOR_RED,
                        "fields": [{"name": "Machine ID", "value": "`" + mid + "`", "inline": True}],
                    })
                _agent_watch_state[mid] = (name, online)
        except Exception as e:
            log.warning(f"Agent watcher error: {e}")
        time.sleep(30)
threading.Thread(target=agent_watch_loop, daemon=True).start()

class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Skip noisy long-poll log lines
        if "/api/control/poll/" in self.path and args and args[1] == "200":
            return
        log.info(f"{self.command} {self.path} -> {args[1]}")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Lockout-Override")
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

    def do_HEAD(self):
        # UptimeRobot's free tier uses HEAD requests. Treat HEAD as a cheap
        # liveness check — return 200 with no body (RFC 7231 §4.3.2).
        # This keeps Render's free-tier service warm without doing any DB work.
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            return self._send_text(200, "PP Bridge cloud — alive")
        if u.path == "/upload":
            return self._upload_form()
        # ─── Emergency kill switch (admin) ─────────────────────────────
        # GET-based so a phone bookmark or Discord-pinned link is one tap.
        # Token comes from query string, compared to KILL_SWITCH_TOKEN env var.
        # If token env var is unset, endpoints are disabled (safe default).
        if u.path in ("/api/admin/lockdown", "/api/admin/unlock", "/api/admin/status"):
            from urllib.parse import parse_qs
            qs = parse_qs(u.query or "")
            given = (qs.get("token", [""])[0] or "").strip()
            if not KILL_SWITCH_TOKEN:
                return self._send_json(503, {"error": "kill_switch_disabled",
                    "message": "Set KILL_SWITCH_TOKEN env var on Render to enable."})
            if given != KILL_SWITCH_TOKEN:
                # Don't leak whether the path exists — return generic 404
                log.warning(f"Kill switch wrong token from {client_ip(self)}")
                return self._send_json(404, {"error": "not_found"})
            ip = client_ip(self)
            ua = short_ua(self.headers.get("User-Agent", ""))
            if u.path == "/api/admin/status":
                return self._send_json(200, {
                    "killswitch_active": is_killswitch_active(),
                    "lockout_active": is_locked_now(),
                })
            if u.path == "/api/admin/lockdown":
                set_killswitch(True)
                log.warning(f"🚨 KILL SWITCH ACTIVATED from {ip}")
                discord_post("gate", {
                    "title": "🚨 EMERGENCY LOCKDOWN ACTIVATED",
                    "color": COLOR_RED,
                    "fields": [
                        {"name": "From", "value": ip, "inline": True},
                        {"name": "User-Agent", "value": ua or "—", "inline": False},
                        {"name": "Status", "value": "All uploads + remote actions BLOCKED. "
                                                    "No override password works while lockdown is active. "
                                                    "Visit /api/admin/unlock?token=… to lift.",
                         "inline": False},
                    ],
                })
                return self._send_text(200,
                    "🚨 Emergency lockdown ACTIVE. All uploads + remote actions are blocked. "
                    "Visit /api/admin/unlock?token=YOUR_TOKEN to lift.")
            if u.path == "/api/admin/unlock":
                set_killswitch(False)
                log.warning(f"✅ KILL SWITCH LIFTED from {ip}")
                discord_post("gate", {
                    "title": "✅ Emergency lockdown lifted",
                    "color": COLOR_GREEN,
                    "fields": [
                        {"name": "From", "value": ip, "inline": True},
                        {"name": "User-Agent", "value": ua or "—", "inline": False},
                        {"name": "Status", "value": "Normal operation restored.", "inline": False},
                    ],
                })
                return self._send_text(200, "✅ Lockdown lifted. Normal operation restored.")
        # ───────────────────────────────────────────────────────────────
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
                                   "presentations": pres,
                                   "playlists": get_playlists_for_machine(r["machine_id"])})
                jobs = [{"job_id": j["job_id"], "machine_id": j["machine_id"],
                         "name": j["name"], "status": j["status"], "result": j["result"]}
                        for j in c.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 30")]
            return self._send_json(200, {"agents": agents, "jobs": jobs, "lockout": is_locked_now()})
        if u.path == "/api/playlists":
            # Optional ?machine_id=… query string — if missing, return all configs.
            from urllib.parse import parse_qs
            qs = parse_qs(u.query or "")
            mid = (qs.get("machine_id", [""])[0] or "").strip()
            if mid:
                return self._send_json(200, {
                    "machine_id": mid,
                    "playlists": get_playlists_for_machine(mid),
                })
            # No machine_id → return everything (handy for debugging)
            out = {}
            for k, v in PLAYLIST_CONFIG.items():
                out[k] = v.get("playlists", [])
            return self._send_json(200, {"all": out})
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
                       "library_adds": library_adds,
                       "playlist_uuid": row["playlist_uuid"] or ""}
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
                # Pull job + agent details for Discord notification
                row = c.execute("SELECT j.name, j.machine_id, a.name AS mac_name "
                                "FROM jobs j LEFT JOIN agents a ON j.machine_id=a.machine_id "
                                "WHERE j.job_id=?", (jid,)).fetchone()
                c.commit()
            log.info(f"  -> Job {jid[:8]} {'done' if ok else 'failed'}: {msg}")
            try:
                pres_name = row["name"] if row else "?"
                mac_label = (row["mac_name"] if row and row["mac_name"] else (row["machine_id"] if row else "?"))
                discord_post("uploads", {
                    "title": ("✅ Upload completed" if ok else "❌ Upload failed"),
                    "color": COLOR_GREEN if ok else COLOR_RED,
                    "fields": [
                        {"name": "To", "value": mac_label, "inline": True},
                        {"name": "Presentation", "value": pres_name[:200], "inline": True},
                        {"name": "Result", "value": (msg or "—")[:900], "inline": False},
                    ],
                })
            except Exception as e:
                log.warning(f"Discord upload-completion notification failed: {e}")
            return self._send_json(200, {"ok": True})
        if u.path.startswith("/api/control/result/"):
            return self._control_result()
        if u.path == "/api/control":
            if is_request_blocked(self.headers):
                return self._send_json(423, {"error": "service_lockout",
                    "message": "Remote is locked during Sunday service hours."})
            return self._control_submit()
        if u.path == "/api/jobs":
            if is_request_blocked(self.headers):
                return self._send_json(423, {"error": "service_lockout",
                    "message": "Uploads are locked during Sunday service hours."})
            return self._submit_job()
        if u.path == "/api/sync_presentation":
            if is_request_blocked(self.headers):
                return self._send_json(423, {"error": "service_lockout",
                    "message": "Sync is locked during Sunday service hours."})
            return self._sync_presentation()
        if u.path == "/api/gate-event":
            # Frontend reports a gate password attempt (success or fail).
            # Body: {"success": true|false, "page": "control"|"index", "attempt_hash": "<sha256>"}
            try:
                b = self._read_json() or {}
            except Exception:
                b = {}
            success = bool(b.get("success"))
            page = (b.get("page") or "")[:30]
            attempt_hash = (b.get("attempt_hash") or "")[:64]
            ua = short_ua(self.headers.get("User-Agent", ""))
            try:
                fields = [
                    {"name": "From", "value": client_ip(self), "inline": True},
                    {"name": "Page", "value": "/" + page if page else "?", "inline": True},
                    {"name": "Status", "value": ("✅ Unlocked" if success else "❌ Failed"), "inline": True},
                ]
                if ua:
                    fields.append({"name": "User-Agent", "value": ua, "inline": False})
                if not success and attempt_hash:
                    # Only the hash, never the plaintext password
                    fields.append({"name": "Attempt hash", "value": "`" + attempt_hash[:16] + "…`", "inline": False})
                discord_post("gate", {
                    "title": "🔓 Gate unlocked" if success else "❌ Gate attempt failed",
                    "color": COLOR_GREEN if success else COLOR_RED,
                    "fields": fields,
                })
            except Exception as e:
                log.warning(f"Discord gate notification failed: {e}")
            return self._send_json(200, {"ok": True})
        return self._send_json(404, {"error": "not found"})

    # =========================================================================
    # CONTROL JOBS — synchronous remote-control over the agent's bridge.py
    # =========================================================================
    # Allowed commands the frontend can send. Anything not in this list is rejected.
    CONTROL_COMMANDS = {
        # Legacy (Ministries-scoped) — kept so the current frontend keeps working
        "list_ministries":      {"args": 0, "max_wait": 10},
        "delete_from_min":      {"args": 1, "max_wait": 12},
        "reorder_min":          {"args": 1, "max_wait": 12},
        "trigger_slide":        {"args": 2, "max_wait": 6},
        # Multi-playlist (parameterized) — used by the new control.html
        "list_playlist_items":  {"args": 1, "max_wait": 10},
        "delete_from_pl":       {"args": 2, "max_wait": 12},
        "reorder_pl":           {"args": 2, "max_wait": 12},
        "trigger_slide_pl":     {"args": 3, "max_wait": 6},
        # Playlist-agnostic — same for everyone
        "list_playlists":       {"args": 0, "max_wait": 10},
        "get_slides":           {"args": 1, "max_wait": 10},
        "get_thumbnails_bulk":  {"args": 1, "max_wait": 14},
        "get_active_thumbnail": {"args": 0, "max_wait": 6},
        "trigger_next":         {"args": 0, "max_wait": 6},
        "trigger_previous":     {"args": 0, "max_wait": 6},
        "clear_slide":          {"args": 0, "max_wait": 6},
        # Cross-Mac sync (Production GUI)
        "read_pres_for_sync":   {"args": 1, "max_wait": 10},
        "sync_pres_to_playlist":{"args": 3, "max_wait": 25},
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
        # For any *_pl command, args[0] is a playlist UUID. Validate it's whitelisted.
        # This is a defense-in-depth check — the frontend shouldn't send unknown
        # UUIDs, but if something slips through (or a malicious client tries),
        # we reject server-side.
        if cmd in ("list_playlist_items", "delete_from_pl", "reorder_pl", "trigger_slide_pl"):
            if not is_playlist_allowed(mid, args[0]):
                return self._send_json(403, {"error": f"playlist {args[0]} not allowed for machine {mid}"})
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
        # NOTE: don't take db_lock during this hot polling loop — SELECTs are safe
        # under WAL mode and holding the global lock here starves the agent's
        # result POST (which needs the lock to UPDATE the row we're waiting for).
        deadline = time.time() + min(spec["max_wait"], CONTROL_RESULT_TIMEOUT)
        while time.time() < deadline:
            with db() as c:
                row = c.execute("SELECT status, result_json FROM control_jobs WHERE job_id=?",
                                (jid,)).fetchone()
            if row and row["status"] in ("done", "failed"):
                try:
                    result = json.loads(row["result_json"]) if row["result_json"] else None
                except Exception:
                    result = {"ok": False, "error": "malformed result"}
                final_ok = row["status"] == "done" and (result.get("ok", True) if isinstance(result, dict) else True)
                # Discord: remote control event
                try:
                    mac_label = ag["name"] if ag else mid
                    args_summary = ", ".join(args)[:100] if args else "—"
                    summary = ""
                    if isinstance(result, dict):
                        if "items" in result and isinstance(result["items"], list):
                            summary = f"{len(result['items'])} items returned"
                        elif "removed" in result:
                            summary = f"Removed: {result.get('removed', '')[:80]}"
                        elif "action" in result:
                            summary = f"Action: {result.get('action', '')}"
                        elif not final_ok:
                            summary = f"Error: {result.get('error', 'unknown')[:200]}"
                    fields = [
                        {"name": "From", "value": client_ip(self), "inline": True},
                        {"name": "Mac", "value": mac_label, "inline": True},
                        {"name": "Status", "value": "✅" if final_ok else "❌", "inline": True},
                        {"name": "Command", "value": f"`{cmd}`" + ((" " + args_summary) if args else ""), "inline": False},
                    ]
                    if summary:
                        fields.append({"name": "Result", "value": summary[:1000], "inline": False})
                    discord_post("remote", {
                        "title": "🎯 Remote action",
                        "color": COLOR_BLUE if final_ok else COLOR_RED,
                        "fields": fields,
                    })
                except Exception as e:
                    log.warning(f"Discord remote notification failed: {e}")
                return self._send_json(200, {
                    "job_id": jid,
                    "ok": final_ok,
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
            # Cheap unlocked SELECT first — only acquire global lock to claim a job
            with db() as c:
                row = c.execute("SELECT * FROM control_jobs WHERE machine_id=? AND status='queued' "
                                "ORDER BY created_at LIMIT 1", (mid,)).fetchone()
            if row:
                # Got a candidate — claim it under the lock
                with db_lock, db() as c:
                    claim = c.execute("UPDATE control_jobs SET status='dispatched', updated_at=? "
                                      "WHERE job_id=? AND status='queued'",
                                      (now(), row["job_id"]))
                    c.commit()
                    if claim.rowcount > 0:
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

    def _sync_presentation(self):
        """Orchestrate a cross-Mac sync. Body:
        {
          "source_machine_id": "building-c-side-screens",
          "source_pres_uuid": "DB956B6B-...",
          "source_name": "Build My Life",  // for display
          "dest_machine_id": "building-c-led-wall",
          "dest_playlist_uuid": "A42AF03D-..."
        }
        Returns the result of the destination's sync_pres_to_playlist call.
        """
        b = self._read_json()
        src_mid = (b.get("source_machine_id") or "").strip()
        src_pres = (b.get("source_pres_uuid") or "").strip()
        src_name = (b.get("source_name") or "Untitled").strip()
        dest_mid = (b.get("dest_machine_id") or "").strip()
        dest_pl = (b.get("dest_playlist_uuid") or "").strip()
        if not (src_mid and src_pres and dest_mid and dest_pl):
            return self._send_json(400, {"error": "need source_machine_id, source_pres_uuid, dest_machine_id, dest_playlist_uuid"})
        # Validate the destination playlist is whitelisted for the destination Mac
        if not is_playlist_allowed(dest_mid, dest_pl):
            return self._send_json(403, {"error": f"playlist {dest_pl} not allowed for machine {dest_mid}"})
        # Verify both agents exist + online
        with db_lock, db() as c:
            src_ag = c.execute("SELECT * FROM agents WHERE machine_id=?", (src_mid,)).fetchone()
            dest_ag = c.execute("SELECT * FROM agents WHERE machine_id=?", (dest_mid,)).fetchone()
        if not src_ag: return self._send_json(404, {"error": f"unknown source machine {src_mid}"})
        if not dest_ag: return self._send_json(404, {"error": f"unknown dest machine {dest_mid}"})
        if not is_online_row(src_ag): return self._send_json(503, {"error": "source machine offline"})
        if not is_online_row(dest_ag): return self._send_json(503, {"error": "destination machine offline"})

        # Step 1: ask source Mac to read the presentation structure
        read_result = self._run_control_sync(src_mid, "read_pres_for_sync", [src_pres], 12)
        if not read_result or not read_result.get("ok"):
            err = (read_result or {}).get("error", "source read failed")
            return self._send_json(500, {"error": f"source read failed: {err}"})
        slides = read_result.get("slides", [])
        if not slides:
            return self._send_json(500, {"error": "source presentation has no slides to sync"})
        canonical_name = read_result.get("name") or src_name

        # Step 2: ask destination Mac to build the new presentation
        slides_json = json.dumps(slides)
        # Guard against very large payloads (RTF can be heavy)
        if len(slides_json) > 800000:
            return self._send_json(500, {"error": f"slides payload too large ({len(slides_json)} bytes)"})
        build_result = self._run_control_sync(dest_mid, "sync_pres_to_playlist",
                                              [canonical_name, dest_pl, slides_json], 25)
        if not build_result or not build_result.get("ok"):
            err = (build_result or {}).get("error", "destination build failed")
            return self._send_json(500, {"error": f"destination build failed: {err}"})

        # Discord notification — sync events go to #remote
        try:
            discord_post("remote", {
                "title": "🔁 Sync completed",
                "color": COLOR_BLUE,
                "fields": [
                    {"name": "From", "value": src_ag["name"] if src_ag else src_mid, "inline": True},
                    {"name": "To", "value": dest_ag["name"] if dest_ag else dest_mid, "inline": True},
                    {"name": "Source", "value": canonical_name, "inline": False},
                    {"name": "Created", "value": build_result.get("created_name", "?") +
                        (" (renamed)" if build_result.get("renamed") else ""), "inline": True},
                    {"name": "Slides", "value": str(build_result.get("slides_count", "?")), "inline": True},
                ],
            })
        except Exception:
            pass

        return self._send_json(200, {
            "ok": True,
            "source_name": canonical_name,
            "created_name": build_result.get("created_name"),
            "renamed": build_result.get("renamed", False),
            "slides_count": build_result.get("slides_count"),
        })

    def _run_control_sync(self, machine_id, command, args, max_wait):
        """Helper: enqueue a control job and synchronously wait for the agent's result.
        Used by _sync_presentation to chain multiple agent calls. Returns the result dict
        (or None on timeout/failure)."""
        jid = uuid.uuid4().hex
        ts = now()
        with db_lock, db() as c:
            c.execute("INSERT INTO control_jobs(job_id, machine_id, command, args_json, status, "
                      "result_json, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                      (jid, machine_id, command, json.dumps(args), "queued", "", ts, ts))
            c.commit()
        deadline = time.time() + max_wait
        while time.time() < deadline:
            with db_lock, db() as c:
                row = c.execute("SELECT status, result_json FROM control_jobs WHERE job_id=?",
                                (jid,)).fetchone()
            if row and row["status"] in ("done", "failed"):
                try:
                    return json.loads(row["result_json"]) if row["result_json"] else None
                except Exception:
                    return {"ok": False, "error": "malformed result"}
            time.sleep(0.15)
        return {"ok": False, "error": f"timeout after {max_wait}s"}
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
        playlist_uuid = ""
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
            elif field_name == "playlist_uuid":
                playlist_uuid = content.decode().strip()
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
        # Validate playlist_uuid against PLAYLIST_CONFIG. Empty string is allowed
        # (means "use default Ministries" via legacy bridge.py path).
        if playlist_uuid and not is_playlist_allowed(machine_id, playlist_uuid):
            return self._send_json(403, {"error": f"playlist {playlist_uuid} not allowed for machine {machine_id}"})
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
                      "library_adds_json, playlist_uuid, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (jid, machine_id, display_name, "queued", jdir,
                       json.dumps(saved), json.dumps(library_adds), playlist_uuid, ts, ts))
            c.commit()
        log.info(f"  -> Job {jid[:8]} queued for {machine_id}: '{display_name}' "
                 f"({len(saved)} files, {len(library_adds)} library_adds, "
                 f"playlist={playlist_uuid[:8] if playlist_uuid else 'default'})")
        # Discord: upload received
        try:
            with db_lock, db() as c:
                ag = c.execute("SELECT name FROM agents WHERE machine_id=?", (machine_id,)).fetchone()
            mac_label = ag["name"] if ag else machine_id
            # Find the playlist's display name for the Discord message
            pl_label = "Ministries (default)"
            if playlist_uuid:
                for pl in get_playlists_for_machine(machine_id):
                    if pl.get("uuid", "").lower() == playlist_uuid.lower():
                        pl_label = pl.get("name", playlist_uuid[:8])
                        break
                else:
                    pl_label = playlist_uuid[:8]
            file_summary = ", ".join(saved[:8]) + (f" (+{len(saved)-8} more)" if len(saved) > 8 else "")
            adds_summary = ", ".join(library_adds[:8]) + (f" (+{len(library_adds)-8} more)" if len(library_adds) > 8 else "")
            fields = [
                {"name": "From", "value": client_ip(self), "inline": True},
                {"name": "To", "value": mac_label, "inline": True},
                {"name": "Playlist", "value": pl_label, "inline": True},
                {"name": "Status", "value": "📥 Received, queued", "inline": False},
                {"name": "Presentation", "value": display_name[:200], "inline": False},
            ]
            if saved:
                fields.append({"name": f"New files ({len(saved)})", "value": file_summary[:1000] or "—", "inline": False})
            if library_adds:
                fields.append({"name": f"Adding existing ({len(library_adds)})", "value": adds_summary[:1000], "inline": False})
            discord_post("uploads", {
                "title": "📤 Upload received",
                "color": COLOR_GRAY,
                "fields": fields,
            })
        except Exception as e:
            log.warning(f"Discord upload-received notification failed: {e}")
        return self._send_json(200, {"job_id": jid, "status": "queued"})


if __name__ == "__main__":
    log.info(f"PP Bridge cloud listening on :{PORT}")
    log.info(f"DB: {DB_PATH}")
    log.info(f"Uploads: {UPLOAD_DIR}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()

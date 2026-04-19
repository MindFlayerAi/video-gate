"""
Video Gate — A simple Flask app to gate video links behind email verification.

Setup:
  1. pip install flask python-dotenv
  2. Edit the .env file to set your admin password and secret key
  3. python app.py
  4. Visit http://localhost:5000

Admin panel: http://localhost:5000/admin
  - Add/remove approved emails
  - Add/remove videos (Telegram, Proton, YouTube — any link works)

Gate links to share on Patreon:
  http://localhost:5000/gate/<video_id>
"""

import os
import json
import sqlite3
import hashlib
import secrets
import threading
import time
import logging
from pathlib import Path
from functools import wraps
from urllib.parse import urlencode
import requests as http_requests
from flask import (
    Flask, request, jsonify, redirect, render_template,
    session, url_for, g
)

# ─── Load .env file ───────────────────────────────────────────────
# Reads key=value pairs from .env so you don't have to set
# environment variables manually. Just edit the .env file.
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and value:
            os.environ.setdefault(key, value)

# ─── Configuration ─────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-to-a-random-string")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")
LOGO_URL = os.environ.get("LOGO_URL", "")
PATREON_ACCESS_TOKEN = os.environ.get("PATREON_ACCESS_TOKEN", "")
PATREON_TIER_NAME = os.environ.get("PATREON_TIER_NAME", "Premium")
PATREON_SYNC_INTERVAL = int(os.environ.get("PATREON_SYNC_INTERVAL", "600"))  # seconds
TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL", "").strip()
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
DATABASE = os.path.join(os.path.dirname(__file__), "gate.db")
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "svg", "webp"}


# ─── Database helpers ──────────────────────────────────────────────
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db:
        db.close()


def init_db():
    db = sqlite3.connect(DATABASE)
    db.execute("""
        CREATE TABLE IF NOT EXISTS emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            source TEXT DEFAULT 'manual',
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS videos (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Add source column if upgrading from older version
    try:
        db.execute("ALTER TABLE emails ADD COLUMN source TEXT DEFAULT 'manual'")
    except sqlite3.OperationalError:
        pass  # column already exists
    db.commit()
    db.close()


# ═══════════════════════════════════════════════════════════════════
# Turso backup/restore layer (plain HTTP — no libsql library)
#
# Uses Turso's Hrana /v2/pipeline HTTP endpoint to push/pull data.
# Local sqlite3 is the source of truth while the app is running;
# Turso is a persistent backup so data survives Render redeploys.
# ═══════════════════════════════════════════════════════════════════
turso_log = logging.getLogger("turso")
last_turso_status = {"time": None, "status": "never", "detail": ""}
_turso_lock = threading.Lock()


def _turso_endpoint():
    """Convert libsql://host → https://host/v2/pipeline."""
    if not TURSO_DATABASE_URL:
        return None
    url = TURSO_DATABASE_URL
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://"):]
    elif url.startswith("wss://"):
        url = "https://" + url[len("wss://"):]
    elif url.startswith("ws://"):
        url = "http://" + url[len("ws://"):]
    return url.rstrip("/") + "/v2/pipeline"


def turso_enabled():
    return bool(TURSO_DATABASE_URL and TURSO_AUTH_TOKEN)


def _py_to_hrana(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": "1" if v else "0"}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    if isinstance(v, (bytes, bytearray)):
        import base64
        return {"type": "blob", "base64": base64.b64encode(bytes(v)).decode()}
    return {"type": "text", "value": str(v)}


def _hrana_to_py(cell):
    t = cell.get("type")
    if t == "null":
        return None
    if t == "integer":
        try:
            return int(cell.get("value"))
        except (TypeError, ValueError):
            return cell.get("value")
    if t == "float":
        return float(cell.get("value"))
    if t == "blob":
        import base64
        return base64.b64decode(cell.get("base64", ""))
    return cell.get("value")


def turso_exec(statements, timeout=30):
    """
    Run a list of (sql, args) tuples as one pipeline.
    Returns a list of result dicts: [{cols:[...], rows:[[...], ...]}, ...].
    Raises on HTTP or statement error.
    """
    endpoint = _turso_endpoint()
    if not endpoint:
        raise RuntimeError("TURSO_DATABASE_URL not set")
    requests_ = []
    for sql, args in statements:
        requests_.append({
            "type": "execute",
            "stmt": {
                "sql": sql,
                "args": [_py_to_hrana(a) for a in (args or [])],
            },
        })
    requests_.append({"type": "close"})
    r = http_requests.post(
        endpoint,
        headers={"Authorization": f"Bearer {TURSO_AUTH_TOKEN}"},
        json={"requests": requests_},
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    results = []
    for i, res in enumerate(data.get("results", [])):
        if res.get("type") == "error":
            err = res.get("error", {})
            raise RuntimeError(f"Turso error on stmt {i}: {err.get('message', err)}")
        if res.get("type") == "ok":
            resp = res.get("response", {})
            if resp.get("type") == "execute":
                result = resp.get("result", {})
                cols = [c.get("name") for c in result.get("cols", [])]
                rows = [
                    [_hrana_to_py(cell) for cell in row]
                    for row in result.get("rows", [])
                ]
                results.append({"cols": cols, "rows": rows})
            else:
                results.append(None)
    return results


def _ensure_turso_schema():
    turso_exec([
        ("""CREATE TABLE IF NOT EXISTS emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            source TEXT DEFAULT 'manual',
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""", []),
        ("""CREATE TABLE IF NOT EXISTS videos (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""", []),
    ])


def turso_backup():
    """Push local emails + videos to Turso (full replace)."""
    global last_turso_status
    if not turso_enabled():
        return False
    with _turso_lock:
        try:
            db = sqlite3.connect(DATABASE)
            db.row_factory = sqlite3.Row
            emails = db.execute(
                "SELECT email, source, added_at FROM emails"
            ).fetchall()
            videos = db.execute(
                "SELECT id, title, url, added_at FROM videos"
            ).fetchall()
            db.close()

            _ensure_turso_schema()

            stmts = [
                ("DELETE FROM emails", []),
                ("DELETE FROM videos", []),
            ]
            for e in emails:
                stmts.append((
                    "INSERT INTO emails (email, source, added_at) VALUES (?, ?, ?)",
                    [e["email"], e["source"] or "manual", e["added_at"]],
                ))
            for v in videos:
                stmts.append((
                    "INSERT INTO videos (id, title, url, added_at) VALUES (?, ?, ?, ?)",
                    [v["id"], v["title"], v["url"], v["added_at"]],
                ))
            # Run in chunks to stay under pipeline size limits
            CHUNK = 100
            # First chunk carries the two DELETEs
            head, tail = stmts[:2], stmts[2:]
            turso_exec(head)
            for i in range(0, len(tail), CHUNK):
                turso_exec(tail[i:i + CHUNK])

            last_turso_status = {
                "time": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
                "status": "ok",
                "detail": f"Backed up {len(emails)} email(s), {len(videos)} video(s)",
            }
            turso_log.info(last_turso_status["detail"])
            return True
        except Exception as e:
            last_turso_status = {
                "time": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
                "status": "error",
                "detail": str(e),
            }
            turso_log.error(f"Turso backup failed: {e}")
            return False


def turso_restore():
    """
    Pull data from Turso into local gate.db.
    Only restores if Turso has at least one row (so a fresh Turso
    doesn't wipe a populated local DB).
    """
    global last_turso_status
    if not turso_enabled():
        return False
    with _turso_lock:
        try:
            _ensure_turso_schema()
            results = turso_exec([
                ("SELECT email, source, added_at FROM emails", []),
                ("SELECT id, title, url, added_at FROM videos", []),
            ])
            email_rows = results[0]["rows"] if results and results[0] else []
            video_rows = results[1]["rows"] if len(results) > 1 and results[1] else []

            if not email_rows and not video_rows:
                turso_log.info("Turso is empty — skipping restore")
                last_turso_status = {
                    "time": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
                    "status": "ok",
                    "detail": "Turso empty, nothing to restore",
                }
                return False

            db = sqlite3.connect(DATABASE)
            db.execute("DELETE FROM emails")
            db.execute("DELETE FROM videos")
            for row in email_rows:
                email, source, added_at = row[0], row[1], row[2]
                try:
                    db.execute(
                        "INSERT INTO emails (email, source, added_at) VALUES (?, ?, ?)",
                        (email, source or "manual", added_at),
                    )
                except sqlite3.IntegrityError:
                    pass
            for row in video_rows:
                vid, title, url, added_at = row[0], row[1], row[2], row[3]
                try:
                    db.execute(
                        "INSERT INTO videos (id, title, url, added_at) VALUES (?, ?, ?, ?)",
                        (vid, title, url, added_at),
                    )
                except sqlite3.IntegrityError:
                    pass
            db.commit()
            db.close()

            last_turso_status = {
                "time": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
                "status": "ok",
                "detail": f"Restored {len(email_rows)} email(s), {len(video_rows)} video(s)",
            }
            turso_log.info(last_turso_status["detail"])
            return True
        except Exception as e:
            last_turso_status = {
                "time": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
                "status": "error",
                "detail": str(e),
            }
            turso_log.error(f"Turso restore failed: {e}")
            return False


# ─── Auth decorator ────────────────────────────────────────────────
def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin"):
            if request.is_json:
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated


# ═══════════════════════════════════════════════════════════════════
# Admin routes
# ═══════════════════════════════════════════════════════════════════

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        data = request.get_json() if request.is_json else request.form
        pw = data.get("password", "")
        if pw == ADMIN_PASSWORD:
            session["admin"] = True
            if request.is_json:
                return jsonify({"ok": True})
            return redirect(url_for("admin_panel"))
        if request.is_json:
            return jsonify({"error": "Wrong password"}), 403
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin_panel():
    return render_template("admin.html")


# ── API: Emails ────────────────────────────────────────────────────
@app.route("/api/emails", methods=["GET"])
@admin_required
def list_emails():
    db = get_db()
    rows = db.execute("SELECT id, email, source, added_at FROM emails ORDER BY added_at DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/emails", methods=["POST"])
@admin_required
def add_email():
    data = request.get_json()
    email = data.get("email", "").strip().lower()
    if not email:
        return jsonify({"error": "Email required"}), 400
    db = get_db()
    try:
        db.execute("INSERT INTO emails (email) VALUES (?)", (email,))
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "Email already exists"}), 409
    return jsonify({"ok": True})


@app.route("/api/emails/<int:email_id>", methods=["DELETE"])
@admin_required
def delete_email(email_id):
    db = get_db()
    db.execute("DELETE FROM emails WHERE id = ?", (email_id,))
    db.commit()
    return jsonify({"ok": True})


# ── API: Videos ────────────────────────────────────────────────────
@app.route("/api/videos", methods=["GET"])
@admin_required
def list_videos():
    db = get_db()
    rows = db.execute("SELECT id, title, url, added_at FROM videos ORDER BY added_at DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/videos", methods=["POST"])
@admin_required
def add_video():
    data = request.get_json()
    title = data.get("title", "").strip()
    url = data.get("url", "").strip()
    if not title or not url:
        return jsonify({"error": "Title and URL required"}), 400
    vid = secrets.token_urlsafe(8)
    db = get_db()
    db.execute("INSERT INTO videos (id, title, url) VALUES (?, ?, ?)", (vid, title, url))
    db.commit()
    return jsonify({"ok": True, "id": vid})


@app.route("/api/videos/<video_id>", methods=["DELETE"])
@admin_required
def delete_video(video_id):
    db = get_db()
    db.execute("DELETE FROM videos WHERE id = ?", (video_id,))
    db.commit()
    return jsonify({"ok": True})


# ── API: Logo ──────────────────────────────────────────────────────
def _get_logo_path():
    """Return the path of the current logo file, or None."""
    for f in Path(UPLOAD_FOLDER).iterdir():
        if f.stem == "logo" and f.suffix.lstrip(".") in ALLOWED_EXTENSIONS:
            return f
    return None


@app.route("/api/logo", methods=["POST"])
@admin_required
def upload_logo():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "No file selected"}), 400
    ext = f.filename.rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({"error": "File type not allowed"}), 400
    # Remove old logo if any
    old = _get_logo_path()
    if old:
        old.unlink(missing_ok=True)
    dest = os.path.join(UPLOAD_FOLDER, f"logo.{ext}")
    f.save(dest)
    return jsonify({"ok": True, "url": "/logo"})


@app.route("/api/logo", methods=["DELETE"])
@admin_required
def delete_logo():
    old = _get_logo_path()
    if old:
        old.unlink(missing_ok=True)
    return jsonify({"ok": True})


@app.route("/logo")
def serve_logo():
    logo = _get_logo_path()
    if not logo:
        return "", 204
    from flask import send_file
    return send_file(str(logo))


@app.route("/api/logo/check")
def check_logo():
    return jsonify({"has_logo": _get_logo_path() is not None})


# ═══════════════════════════════════════════════════════════════════
# Gate routes (public-facing)
# ═══════════════════════════════════════════════════════════════════

@app.route("/gate/<video_id>")
def gate_page(video_id):
    db = get_db()
    video = db.execute("SELECT id, title FROM videos WHERE id = ?", (video_id,)).fetchone()
    if not video:
        return render_template("404.html"), 404
    return render_template("gate.html", video=dict(video), logo_url=LOGO_URL)


@app.route("/api/verify", methods=["POST"])
def verify_email():
    data = request.get_json()
    email = data.get("email", "").strip().lower()
    video_id = data.get("video_id", "")
    if not email or not video_id:
        return jsonify({"error": "Missing fields"}), 400

    db = get_db()
    # Check email exists
    row = db.execute("SELECT id FROM emails WHERE email = ?", (email,)).fetchone()
    if not row:
        return jsonify({"granted": False})

    # Check video exists and return URL
    video = db.execute("SELECT url FROM videos WHERE id = ?", (video_id,)).fetchone()
    if not video:
        return jsonify({"error": "Video not found"}), 404

    return jsonify({"granted": True, "redirect_url": video["url"]})


# ─── Home redirect ────────────────────────────────────────────────
@app.route("/")
def index():
    return redirect(url_for("admin_login"))


# ═══════════════════════════════════════════════════════════════════
# Patreon Sync
# ═══════════════════════════════════════════════════════════════════
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("patreon-sync")

PATREON_API = "https://www.patreon.com/api/oauth2/v2"

last_sync_status = {"time": None, "status": "never", "detail": "", "count": 0}


def patreon_get(path, params=None):
    """Make an authenticated GET to the Patreon API v2."""
    headers = {"Authorization": f"Bearer {PATREON_ACCESS_TOKEN}"}
    url = f"{PATREON_API}{path}"
    if params:
        url += "?" + urlencode(params, safe="[](),")
    r = http_requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def get_campaign_id():
    """Fetch the creator's campaign ID."""
    data = patreon_get("/campaigns", {"fields[campaign]": "created_at"})
    campaigns = data.get("data", [])
    if not campaigns:
        raise Exception("No campaigns found for this access token")
    return campaigns[0]["id"]


def get_tier_id(campaign_id, tier_name):
    """Find the tier ID matching the given name."""
    data = patreon_get(f"/campaigns/{campaign_id}", {
        "include": "tiers",
        "fields[tier]": "title,amount_cents",
    })
    tiers = data.get("included", [])
    matching = []
    for t in tiers:
        if t.get("type") == "tier":
            title = t.get("attributes", {}).get("title", "")
            if title.lower() == tier_name.lower():
                matching.append(t["id"])
    return matching


def fetch_patron_emails(campaign_id, tier_ids):
    """Fetch emails of active patrons in the specified tiers."""
    emails = set()
    cursor = None

    while True:
        params = {
            "include": "currently_entitled_tiers,user",
            "fields[member]": "email,patron_status,full_name",
            "fields[tier]": "title",
            "fields[user]": "email",
            "page[count]": "100",
        }
        if cursor:
            params["page[cursor]"] = cursor

        data = patreon_get(f"/campaigns/{campaign_id}/members", params)
        members = data.get("data", [])

        # Build lookup of included resources
        included = {}
        for inc in data.get("included", []):
            included[(inc["type"], inc["id"])] = inc

        for member in members:
            attrs = member.get("attributes", {})
            status = attrs.get("patron_status")

            # Only active patrons
            if status != "active_patron":
                continue

            # Check if they're in one of the target tiers
            entitled = member.get("relationships", {}).get(
                "currently_entitled_tiers", {}
            ).get("data", [])
            member_tier_ids = [t["id"] for t in entitled]

            if not tier_ids or any(tid in tier_ids for tid in member_tier_ids):
                # Get email from member attributes first, fall back to user
                email = attrs.get("email")
                if not email:
                    user_rel = member.get("relationships", {}).get("user", {}).get("data")
                    if user_rel:
                        user = included.get(("user", user_rel["id"]))
                        if user:
                            email = user.get("attributes", {}).get("email")
                if email:
                    emails.add(email.strip().lower())

        # Pagination
        cursors = data.get("meta", {}).get("pagination", {}).get("cursors")
        if cursors and cursors.get("next"):
            cursor = cursors["next"]
        else:
            break

    return emails


def sync_patreon_emails():
    """Main sync function — fetches Patreon patrons and updates the DB."""
    global last_sync_status
    try:
        log.info("Starting Patreon sync...")

        campaign_id = get_campaign_id()
        tier_ids = get_tier_id(campaign_id, PATREON_TIER_NAME)

        if not tier_ids:
            log.warning(f"No tier found matching '{PATREON_TIER_NAME}'. "
                       f"Will sync ALL active patrons.")

        patron_emails = fetch_patron_emails(campaign_id, tier_ids)
        log.info(f"Found {len(patron_emails)} active patron(s) in '{PATREON_TIER_NAME}' tier")

        db = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row

        # Get current patreon-synced emails
        existing = set(
            row["email"] for row in
            db.execute("SELECT email FROM emails WHERE source = 'patreon'").fetchall()
        )

        # Add new patrons
        added = 0
        for email in patron_emails:
            if email not in existing:
                try:
                    db.execute(
                        "INSERT INTO emails (email, source) VALUES (?, 'patreon')",
                        (email,)
                    )
                    added += 1
                except sqlite3.IntegrityError:
                    # Email exists as manual — leave it alone
                    pass

        # Remove former patrons (only patreon-sourced, not manual)
        removed = 0
        for email in existing:
            if email not in patron_emails:
                db.execute(
                    "DELETE FROM emails WHERE email = ? AND source = 'patreon'",
                    (email,)
                )
                removed += 1

        db.commit()
        db.close()

        last_sync_status = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            "status": "ok",
            "detail": f"Added {added}, removed {removed}",
            "count": len(patron_emails),
        }
        log.info(f"Sync complete: {added} added, {removed} removed, "
                f"{len(patron_emails)} total patrons")

        # Auto-backup to Turso after each successful Patreon sync
        if turso_enabled():
            turso_backup()

    except Exception as e:
        last_sync_status = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            "status": "error",
            "detail": str(e),
            "count": 0,
        }
        log.error(f"Patreon sync error: {e}")


_last_sync_fire = 0.0
_sync_fire_lock = threading.Lock()


@app.before_request
def maybe_trigger_sync():
    """Trigger a Patreon sync in the background if the interval has elapsed.
    Request-driven rather than a background loop so it survives Gunicorn
    worker recycling on Render's free tier."""
    global _last_sync_fire
    if not PATREON_ACCESS_TOKEN:
        return
    now = time.time()
    with _sync_fire_lock:
        if now - _last_sync_fire < PATREON_SYNC_INTERVAL:
            return
        _last_sync_fire = now
    threading.Thread(target=sync_patreon_emails, daemon=True).start()


# ── API: Sync status & manual trigger ─────────────────────────────
@app.route("/api/sync/status")
@admin_required
def sync_status():
    return jsonify(last_sync_status)


@app.route("/api/sync/now", methods=["POST"])
@admin_required
def sync_now():
    if not PATREON_ACCESS_TOKEN:
        return jsonify({"error": "PATREON_ACCESS_TOKEN not set"}), 400
    # Run sync in a thread so it doesn't block
    threading.Thread(target=sync_patreon_emails, daemon=True).start()
    return jsonify({"ok": True, "message": "Sync started"})


# ── API: Turso backup/restore ─────────────────────────────────────
@app.route("/api/turso/status")
@admin_required
def turso_status():
    return jsonify({
        "enabled": turso_enabled(),
        **last_turso_status,
    })


@app.route("/api/turso/commit", methods=["POST"])
@admin_required
def turso_commit():
    if not turso_enabled():
        return jsonify({"error": "Turso not configured (set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN)"}), 400
    ok = turso_backup()
    if not ok:
        return jsonify({"error": last_turso_status.get("detail") or "Backup failed"}), 500
    return jsonify({"ok": True, "detail": last_turso_status.get("detail", "")})


@app.route("/api/turso/restore", methods=["POST"])
@admin_required
def turso_restore_endpoint():
    if not turso_enabled():
        return jsonify({"error": "Turso not configured"}), 400
    ok = turso_restore()
    return jsonify({"ok": ok, "detail": last_turso_status.get("detail", "")})


# ─── Run ───────────────────────────────────────────────────────────
init_db()

# Restore from Turso at startup (recovers data after Render redeploys,
# which wipe the ephemeral filesystem and thus gate.db).
if turso_enabled():
    try:
        turso_restore()
    except Exception as e:
        turso_log.error(f"Startup restore failed (continuing): {e}")
else:
    turso_log.info("Turso not configured — backup/restore disabled")

# Run one immediate sync at startup, then request-driven every PATREON_SYNC_INTERVAL
if PATREON_ACCESS_TOKEN:
    _t = threading.Thread(target=sync_patreon_emails, daemon=True)
    _t.start()
    log.info(f"Patreon sync started (interval {PATREON_SYNC_INTERVAL}s, "
             f"tier '{PATREON_TIER_NAME}')")
else:
    log.info("No PATREON_ACCESS_TOKEN set — sync disabled")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    print(f"\n  Video Gate is running on port {port}!")
    print(f"  Admin panel: http://localhost:{port}/admin\n")
    app.run(host="0.0.0.0", port=port, debug=debug)

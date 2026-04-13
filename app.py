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
from pathlib import Path
from functools import wraps
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
    db.commit()
    db.close()


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
    rows = db.execute("SELECT id, email, added_at FROM emails ORDER BY added_at DESC").fetchall()
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


# ─── Run ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    print("\n  Video Gate is running!")
    print("  Admin panel: http://localhost:5000/admin")
    print("  Default password: changeme\n")
    app.run(debug=True, port=5000)

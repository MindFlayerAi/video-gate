# Video Gate

A simple, self-hosted app to gate video links (Telegram, Proton, YouTube, etc.) behind email verification. Built for creators who want to share exclusive content with Patreon supporters.

## How It Works

1. **You** add approved email addresses and video links in the admin panel
2. **You** share gate links on Patreon (e.g. `https://yoursite.com/gate/abc123`)
3. **Members** click the link, enter their email
4. If approved → redirected to the video. If not → denied.

## Quick Start

```bash
# Install Flask
pip install flask

# Run the app
python app.py
```

Then visit: **http://localhost:5000**

Default admin password: `changeme`

## Configuration

Set these environment variables before running:

```bash
export SECRET_KEY="some-long-random-string"
export ADMIN_PASSWORD="your-secure-password"
python app.py
```

## Deploying (Free Options)

### Railway.app (recommended for beginners)
1. Push this folder to a GitHub repo
2. Go to [railway.app](https://railway.app), connect your repo
3. Add environment variables: `SECRET_KEY`, `ADMIN_PASSWORD`
4. Deploy — you'll get a public URL like `yourapp.up.railway.app`

### Render.com
1. Push to GitHub
2. Create a new "Web Service" on [render.com](https://render.com)
3. Set build command: `pip install flask`
4. Set start command: `python app.py`
5. Add environment variables

### VPS (DigitalOcean, Linode, etc.)
```bash
pip install flask gunicorn
gunicorn app:app -b 0.0.0.0:5000
```

Use nginx as a reverse proxy + Let's Encrypt for HTTPS.

## File Structure

```
video-gate/
├── app.py              # Flask backend + API
├── gate.db             # SQLite database (auto-created)
├── templates/
│   ├── admin_login.html  # Admin login page
│   ├── admin.html        # Admin panel (manage emails & videos)
│   ├── gate.html         # Gate page (what members see)
│   └── 404.html          # Invalid link page
└── README.md
```

## Security Notes

- Email list and video URLs are stored server-side in SQLite — visitors can't see them
- The verification check happens on the server, so it can't be bypassed in the browser
- For production, always use HTTPS and change the default password
- This uses simple email matching (no login accounts). It's "password-like" security, not full authentication. Good enough for gating casual content, but not for sensitive data.

## Supported Video Sources

Any URL works — the app just redirects after verification:
- Telegram video links
- Proton Drive share links
- YouTube (unlisted videos)
- Vimeo
- Google Drive
- Any direct video URL

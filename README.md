# Telegram Torrent Downloader Bot

A secure, modular, production-ready Python Telegram bot that accepts magnet links and `.torrent` files and downloads them to a hosted environment or Linux server, supporting resumable Google Drive uploads and chunked Telegram uploads.

Written in modern Python 3.10+ using `python-telegram-bot` (v20.x).

## ⚠️ Important Legal Policy
This project must be used for **legal purposes only**, such as downloading open-source Linux distributions, public domain content, or media you legally own.
You are solely responsible for ensuring you comply with the law, as well as the Terms of Service of your cloud provider (e.g., Google Colab, Google Drive, Telegram).
**Do not use this bot to pirate copyrighted material.**
This bot refuses to proceed for non-admin users by default to prevent abuse. **Do not disable admin checks if running on a public server.**

---

## Features
- **Primary Downloader:** `libtorrent` (Python bindings) for speed and DHT support.
- **Fallback Downloader:** `aria2c` fallback using JSON-RPC.
- **Queue Management:** Priority queue backed by a JSON file (`queue_state.json`) that saves progress and state to survive restarts.
- **Uploader:** Resumable Google Drive uploading, MTProto Userbot uploading to Dump Channel, and chunked Telegram file splitting.
- **Storage Management:** Automatic disk usage monitoring and local file cleanup.
- **GCS Persistence:** Resumable Google Cloud Storage (GCS) uploading.
- **Security Default:** Strict Admin ID whitelist out of the box. Only authorized Telegram IDs can issue commands.
- **Structured Logging:** JSON-lines logging to `bot.log` for easy monitoring.
- **Health Server:** Exposes an HTTP `/health` endpoint (FastAPI) useful for monitoring systems.

## Security & Admin Checklist
1. **Admin Whitelist:** Always set `admin_ids` in `config.cfg`. Without this, no commands will work. To relax this (not recommended), you must empty the `admin_ids` list.
2. **Rotating Bot Token:** If your token leaks, message `@BotFather` on Telegram, send `/revoke`, and select your bot to get a new token. Use `utils.rotate_token` or edit `config.cfg`.
3. **Revoking Google OAuth:** If `credentials.json` or `token.json` is compromised, go to Google Cloud Console > APIs & Services > Credentials and delete the OAuth client, then delete `token.json` from your server.
4. **Shared Drives:** Be careful storing tokens (`token.json`, `config.cfg`) on shared Google Drives, as anyone with read access can steal your bot/Drive identity.
5. **MTProto Session Strings:** Generating the session string requires a phone login and is a one-time manual step by a human on their own machine. Keep it secret. Use a dedicated Telegram account for uploads when possible. The account will act like a real user and is subject to Telegram rules. Never commit secrets like `config.cfg` (with `session_string`), `service-account.json`, and any `.session` files. Add them to `.gitignore`.
6. **GCP Billing:** Enabling GCS may incur costs. Add guidance to create a bucket, set retention rules, and monitor billing alerts.
7. **Rate Limits:** MTProto uploads may trigger FloodWaits; the uploader implements exponential backoff and respects server wait messages.

---

## 🚀 Setup A: Self-hosted Linux Server (systemd)

### 1. Install Dependencies
Ensure you have Python 3.10+, `pip`, and `aria2c` installed.
```bash
sudo apt update
sudo apt install python3 python3-pip aria2 python3-libtorrent
```

### 2. Configure the Bot
Clone the repo and install Python requirements:
```bash
pip install -r requirements.txt
cp config.cfg.example config.cfg
```
Edit `config.cfg` and fill in:
- `admin_ids`: Your Telegram user ID (Get it from `@userinfobot`).
- `token`: Your Bot Token from `@BotFather`.
- Drive folder ID and credentials path.

### 3. Run as a Systemd Service
Create a new service file: `sudo nano /etc/systemd/system/telegram-torrent-bot.service`
```ini
[Unit]
Description=Telegram Torrent Bot Server
After=network.target

[Service]
User=your_linux_user
WorkingDirectory=/path/to/project
ExecStart=/usr/bin/python3 server.py
Restart=always

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable telegram-torrent-bot
sudo systemctl start telegram-torrent-bot
```

---

## 🚀 Setup B: Google Colab Environment

You can run this directly in a Google Colab notebook. Note that Colab runtimes shut down when you close the browser or after a certain timeout. Ensure your Colab instance is kept open, or configure your queue manager to save state aggressively to Drive so a restarted runtime can pick up where it left off.

### Colab Snippet
Open a new notebook and paste this into a cell:

```python
# 1. Mount Google Drive
from google.colab import drive
drive.mount('/content/drive')

# 2. Install dependencies (libtorrent and aria2)
!apt-get install python3-libtorrent aria2
!pip install -r /content/drive/MyDrive/bot_folder/requirements.txt

# 3. Change directory to your bot folder and run
import os
os.chdir('/content/drive/MyDrive/bot_folder')

# Ensure your config.cfg is populated in your Drive folder!
!python server.py
```

---

## Logs Example
Structured JSON logs are saved to `bot.log`. Example:
```json
{"time": "2023-10-25 10:00:00,000", "level": "INFO", "name": "root", "message": "Added task e4f1a2 to queue."}
{"time": "2023-10-25 10:00:05,000", "level": "INFO", "name": "root", "message": "Started libtorrent download for task e4f1a2"}
```

## Testing
To run the included test suite:
```bash
pip install pytest
pytest tests/
```

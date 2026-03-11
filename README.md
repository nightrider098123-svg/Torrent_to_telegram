# Telegram Torrent Downloader Bot (Colab/MTProto Uploader)

This project allows you to download torrents directly inside a Google Colab VM disk and instantly stream-upload them to a Telegram Dump Channel using an MTProto User Session. It bypasses the need for external cloud storage and implements robust disk safety checks.

## Features
* **Zero External Cloud Storage:** Downloads live purely on the ephemeral Colab VM disk (`/content/downloads`).
* **Instant MTProto Uploads:** Uses Pyrogram + MTProto session to upload massive files (up to 4 GiB limits or split natively) immediately upon completion.
* **Disk Safety Monitor:** Stops downloads if disk space gets low (configurable, e.g., 70 GB), flushes the upload queue, and resumes downloads when space frees up.
* **Smart File Splitting:** Automatically splits huge files (>3.9 GiB) via streaming logic to save disk space while uploading.
* **Resume Capability:** Saves queued download and upload states to a pinned JSON manifest message in a private Admin Telegram channel, recovering seamlessly across Colab VM restarts.

## Hard Requirements
- **DO NOT** use Google Drive.
- **DO NOT** use Google Cloud Storage.

## Colab Setup Steps

### 1. Get Telegram API Credentials
1. Go to [https://my.telegram.org/apps](https://my.telegram.org/apps) and log in.
2. Create an application to get your `API_ID` (integer) and `API_HASH` (string).

### 2. Generate a User Session String
You need a string session so the bot can upload large files natively as a user:
1. Open a terminal or a local Python environment.
2. Install `pyrogram` and `TgCrypto`:
   ```bash
   pip install pyrogram TgCrypto
   ```
3. Run the following script to log in and get your string:
   ```python
   from pyrogram import Client
   api_id = 1234567 # YOUR API ID
   api_hash = "your_api_hash"
   with Client("my_account", api_id, api_hash) as app:
       print(app.export_session_string())
   ```
4. Save the long string generated; you will need it for the Colab environment variables.
*(Security Warning: This string acts as your password. Do not commit or share it!)*

### 3. Create Telegram Channels
1. **Dump Channel:** Create a new channel where files will be dumped. Add the account you used to generate the session string as an Administrator. Get the Chat ID (e.g. `-1001234567890`).
2. **Admin Channel:** Create a private group or channel for bot state and logs. Get the Chat ID (e.g. `-1009876543210`). Add the session account as an Admin.

### 4. Run the Colab Notebook
Upload the `notebook.ipynb` file from this repository to your Google Colab account.
Execute the cells one by one. The notebook will guide you to set environment variables.

#### Example Environment Variables (Set in Notebook)
```python
import os

# DO NOT SHARE THIS CELL!
os.environ["API_ID"] = "1234567"
os.environ["API_HASH"] = "abcdef1234567890"
os.environ["USER_SESSION_STRING"] = "1BJWap_MBz..._your_long_string_here"

os.environ["DUMP_CHANNEL_ID"] = "-1001234567890"
os.environ["ADMIN_CHANNEL_ID"] = "-1009876543210"

# Optional settings
os.environ["DOWNLOAD_DIR"] = "/content/downloads"
os.environ["MAX_DISK_USED_GB"] = "70.0"
os.environ["RESUME_DISK_USED_GB"] = "40.0"
os.environ["UPLOAD_WORKERS"] = "3"
```

## Admin Commands
Send these messages to the **Admin Channel** to control the bot:
- `ADD <magnet_link>`: Queues a new torrent download.
- `STATUS`: Replies with a summary of disk usage, queue size, and active downloads.
- `PAUSE`: Immediately stops all active aria2 downloads.
- `RESUME`: Resumes all paused aria2 downloads.
- `SET MAX_GB <float>`: Dynamically adjusts the disk safety threshold (e.g., `SET MAX_GB 60`).
- `SHUTDOWN`: Gracefully shuts down the bot.

## Troubleshooting
**1. Session Expired or StringSession Invalid**
If the bot crashes stating `AuthKeyUnregistered` or `SessionExpired`, you must log in again. Re-run the local Pyrogram script to generate a new `USER_SESSION_STRING` and update your Colab env var.

**2. FloodWait or Throttled Uploads**
If logs show continuous `FloodWait` warnings, it means Telegram is throttling your uploads. The bot automatically implements exponential backoff to respect this. Wait for it to clear. Consider lowering `UPLOAD_WORKERS` to `1` or `2` in your config to prevent triggering limits.

**3. Colab Disk Cleared Unexpectedly**
Colab VMs are ephemeral. If your notebook disconnects and clears disk:
1. Re-run the notebook cells.
2. The bot will automatically read the last `STATE_MANIFEST:` message in your Admin Channel.
3. It will re-queue any uncompleted `magnet_links` automatically.
4. If a file was only partially uploaded, it will restart the download and subsequent upload.

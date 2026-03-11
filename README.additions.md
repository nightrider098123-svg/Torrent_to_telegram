## MTProto Uploads and Google Cloud Storage Additions

### Setting up MTProto Userbot Uploads
By default, telegram bot limits uploads to 50MB (via ID) or ~2GB directly using local bot API servers. To natively bypass this limit (up to 2GB or 4GB with Telegram Premium), we use Pyrogram with a real user session.

**SECURITY WARNING**: The session string acts as a password giving full access to your Telegram account. NEVER commit `config.cfg` or `.session` files. Always keep them secret. It is highly recommended to use a dedicated "dummy" Telegram account for uploads.

1. Go to https://my.telegram.org/apps and create an application to obtain your `API ID` and `API Hash`.
2. Run `python userbot_session_helper.py` on your local machine.
3. Follow the prompt to log in and generate the session string.
4. Copy the generated string into your `config.cfg` file under the `[MTProto]` section as `session_string`.
5. Also, define the `dump_channel` id (e.g. `-1001234567890`) where the files should be uploaded.
6. Make sure you add the account corresponding to the session string as an Administrator or a User with Posting privileges to the `dump_channel`.

### Setting up Google Cloud Storage (GCS) Persistence
GCS offers durable long term persistence, completely bypassing local disk ephemeral limitations.

**WARNING**: Using GCS can incur storage and network egress costs on your Google Cloud Billing Account. Be sure to configure lifecycle retention rules on your bucket to automatically prune files if you want to limit spending. NEVER commit the service account JSON.

1. Go to Google Cloud Console, create a new project (or use an existing one), and create a GCS Bucket.
2. Go to IAM & Admin -> Service Accounts -> Create Service Account. Grant it the `Storage Object Admin` role for the bucket.
3. Under keys, add a new JSON key. This will download a JSON file to your PC.
4. Place this JSON file alongside your bot (e.g. `service-account.json`) or anywhere on your host system.
5. In `config.cfg`, set `use_gcs = true`, enter your `gcp_project`, `gcs_bucket`, and the exact path to `gcp_service_account_json`.

### Automatic Storage Management
Because large files can rapidly fill disk space, the system uses an automatic storage manager.
1. In `config.cfg`, under `[Storage]`, define `max_local_usage_gb` (defaults to 70.0).
2. The bot continuously monitors the `download_dir`.
3. If usage exceeds the limit, it locates the *oldest* files that have been successfully uploaded to the dump channel/GCS, and deletes them from the local disk to free space.
4. Information on deleted GBs will be sent directly to your `owner_id`.

### Running the Uploader Manually or via systemd
Since the upload process now hooks seamlessly into the standard queue flow, any completed torrent download will immediately trigger the upload sequence:
1. Metadata message post to dump channel.
2. GCS Upload (if enabled).
3. MTProto Userbot/Bot Upload to dump channel.
4. File flagged for cleanup in local storage manager.
5. Summary message posted.

**To run the system:**
You can start the background queue processor and the webserver by simply running:
```bash
python server.py
```
To run it supervised in the background using `systemd`, refer to the systemd service file setup in the main `README.md`.

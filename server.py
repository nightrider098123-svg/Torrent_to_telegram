import os
import time
import asyncio
import logging
import threading
from typing import Dict, Any

from fastapi import FastAPI
import uvicorn
from telegram.ext import ApplicationBuilder

from bot import start_bot, queue_mgr, downloader, config, is_admin
from queue_manager import Task
from utils import setup_logging, format_size
from uploader import DriveUploader, telegram_chunked_upload
from userbot_uploader import UserbotUploader
from gcs_uploader import GCSUploader
from storage_manager import StorageManager

setup_logging()

# Initialize DriveUploader globally (lazily instantiates based on config)
credentials_file = config.get("Drive", "credentials_file", fallback="credentials.json")
token_file = config.get("Drive", "token_file", fallback="token.json")
drive_uploader = DriveUploader(credentials_file, token_file)

app = FastAPI(title="Telegram Torrent Bot Health Server")

@app.get("/health")
def read_health():
    import shutil
    total, used, free = shutil.disk_usage("/")
    tasks = queue_mgr.get_all_tasks()
    return {
        "status": "ok",
        "disk_free_bytes": free,
        "queue_size": len([t for t in tasks if t.status == "queued"]),
        "active_downloads": len([t for t in tasks if t.status in ["downloading", "uploading"]])
    }

def progress_callback(task: Task):
    # This callback is called by the downloader backend in a separate thread.
    # It updates the task state, which is saved by the queue manager.
    # To reduce disk I/O, we might want to save state less frequently,
    # but for now we'll just let queue_mgr handle it if called manually.
    queue_mgr.save_state()

def worker_loop(application_bot=None, loop=None):
    """Background thread that pops tasks from queue and executes them."""
    logging.info("Started background worker loop.")
    base_download_dir = config.get("Downloader", "download_dir", fallback="./downloads")

    while True:
        task = queue_mgr.get_next_task()
        if not task:
            time.sleep(5)
            continue

        logging.info(f"Starting task {task.task_id}: {task.source}")

        try:
            task_download_dir = os.path.join(base_download_dir, task.task_id)
            os.makedirs(task_download_dir, exist_ok=True)

            if task.status != "completed":
                # Check disk space before starting download
                # We assume 1GB free minimum since we don't know the torrent size yet.
                # (libtorrent gets size asynchronously, aria2c gets it via metadata)
                from utils import check_disk_space
                if not check_disk_space(base_download_dir, 1 * 1024 * 1024 * 1024):
                    logging.error(f"Insufficient disk space for task {task.task_id}")
                    task.error = "Insufficient disk space (>1GB required)"
                    queue_mgr.update_task_status(task.task_id, "failed")
                    continue

                queue_mgr.update_task_status(task.task_id, "downloading")
                # We don't block here because downloader backends run in their own threads
                downloader.start_download(task, task_download_dir, progress_callback)

                # Wait for download to finish, with max size checks
                max_auto_size = config.getint("Bot", "max_auto_download_size_gb", fallback=50) * 1024 * 1024 * 1024
                paused_for_admin = False
                while task.status == "downloading":
                    # If we just got the metadata and we know the size, check it
                    if not paused_for_admin and not task.approved_large_file and task.total_bytes > max_auto_size:
                        logging.warning(f"Task {task.task_id} is larger than max auto size ({task.total_bytes} > {max_auto_size}). Pausing.")
                        downloader.cancel(task.task_id) # Cancel the backend download
                        task.status = "waiting_admin_confirmation"
                        queue_mgr.update_task_status(task.task_id, "waiting_admin_confirmation")

                        # Notify admin via Telegram (using asyncio loop in a thread-safe way if possible)
                        if application_bot and loop:
                            admin_ids = config.get("Bot", "admin_ids", fallback="").split(",")
                            for a_id in admin_ids:
                                if a_id.strip():
                                    try:
                                        msg = f"Task `{task.task_id}` exceeds max auto download size.\nUse `/confirm {task.task_id}` to force download."
                                        asyncio.run_coroutine_threadsafe(
                                            application_bot.send_message(chat_id=int(a_id.strip()), text=msg, parse_mode="Markdown"),
                                            loop
                                        )
                                    except Exception:
                                        pass
                        break
                    time.sleep(5)

                if task.status == "cancelled":
                    logging.info(f"Task {task.task_id} was cancelled.")
                    continue

                if task.status == "failed":
                    logging.error(f"Task {task.task_id} failed: {task.error}")
                    if application_bot and loop:
                        admin_ids = config.get("Bot", "admin_ids", fallback="").split(",")
                        for a_id in admin_ids:
                            if a_id.strip():
                                try:
                                    msg = f"⚠️ *Error* on task `{task.task_id}`:\n{task.error}"
                                    asyncio.run_coroutine_threadsafe(
                                        application_bot.send_message(chat_id=int(a_id.strip()), text=msg, parse_mode="Markdown"),
                                        loop
                                    )
                                except Exception:
                                    pass
                    continue

            # If completed (or already completed from a local file upload)
            if task.status == "completed":
                queue_mgr.update_task_status(task.task_id, "uploading")
                logging.info(f"Task {task.task_id} ready for upload.")

                # To get the exact downloaded file, we approximate from source for local files
                # or download_dir if it's a torrent. For torrents, we use the isolated task directory.
                file_to_upload = task.source if task.type == "local_upload" else task_download_dir

                # 1. Gather all files to upload
                files_to_upload = []
                if os.path.isfile(file_to_upload):
                    files_to_upload.append(file_to_upload)
                elif os.path.isdir(file_to_upload):
                    for root, _, files in os.walk(file_to_upload):
                        for file in files:
                            files_to_upload.append(os.path.join(root, file))

                # Reload config dynamically
                from utils import load_config
                current_config = load_config()

                # Check new configuration sections
                use_userbot = current_config.getboolean("Upload", "use_userbot", fallback=False)
                dump_channel_str = current_config.get("Upload", "dump_channel", fallback="")
                dump_channel = int(dump_channel_str) if dump_channel_str else None
                owner_id_str = current_config.get("Telegram", "owner_id", fallback="")
                owner_id = int(owner_id_str) if owner_id_str else None

                use_gcs = current_config.getboolean("Storage", "use_gcs", fallback=False)

                # Send initial metadata message to dump channel and owner
                metadata_msg = (
                    f"📦 **New Download Completed**\n"
                    f"**Task ID:** `{task.task_id}`\n"
                    f"**Source:** `{task.source}`\n"
                    f"**Total Size:** {format_size(task.total_bytes)}\n"
                    f"**Files:** {len(files_to_upload)}\n"
                )

                dump_metadata_message_id = None
                if application_bot and loop:
                    if dump_channel:
                        try:
                            future = asyncio.run_coroutine_threadsafe(
                                application_bot.send_message(chat_id=dump_channel, text=metadata_msg, parse_mode="Markdown"),
                                loop
                            )
                            msg_obj = future.result()
                            dump_metadata_message_id = msg_obj.message_id
                        except Exception as e:
                            logging.error(f"Failed to send metadata message to dump channel: {e}")

                    if owner_id:
                        try:
                            asyncio.run_coroutine_threadsafe(
                                application_bot.send_message(chat_id=owner_id, text=metadata_msg, parse_mode="Markdown"),
                                loop
                            )
                        except Exception as e:
                            logging.error(f"Failed to send metadata message to owner: {e}")

                # Initialize uploaders if needed
                userbot_uploader = None
                if use_userbot and dump_channel:
                    session_string = current_config.get("MTProto", "session_string", fallback="")
                    api_id = current_config.getint("MTProto", "api_id", fallback=0)
                    api_hash = current_config.get("MTProto", "api_hash", fallback="")
                    max_retries = current_config.getint("Upload", "max_retries", fallback=5)
                    telegram_file_limit_bytes = current_config.getint("Upload", "telegram_file_limit_bytes", fallback=2147483648)

                    if session_string and api_id and api_hash:
                        userbot_uploader = UserbotUploader(
                            session_string=session_string,
                            api_id=api_id,
                            api_hash=api_hash,
                            dump_channel=dump_channel,
                            max_retries=max_retries,
                            telegram_file_limit_bytes=telegram_file_limit_bytes
                        )

                gcs_uploader = None
                if use_gcs:
                    gcp_project = current_config.get("Storage", "gcp_project", fallback="")
                    gcs_bucket = current_config.get("Storage", "gcs_bucket", fallback="")
                    service_account_json = current_config.get("Storage", "gcp_service_account_json", fallback="service-account.json")
                    if gcp_project and gcs_bucket and service_account_json:
                        gcs_uploader = GCSUploader(gcp_project, gcs_bucket, service_account_json)

                # Get a mock context for telegram_chunked_upload fallback
                class MockContext:
                    def __init__(self, bot):
                        self.bot = bot
                context = MockContext(application_bot) if application_bot else None

                # Storage Manager to mark files
                download_dir = current_config.get("Storage", "download_dir", fallback="./downloads")
                max_local_usage_gb = current_config.getfloat("Storage", "max_local_usage_gb", fallback=70.0)
                storage_manager = StorageManager(download_dir=download_dir, max_local_usage_gb=max_local_usage_gb)

                final_status = {}
                gcs_urls = {}

                for f_path in files_to_upload:
                    file_name = os.path.basename(f_path)
                    file_success = True

                    # 2. Upload to GCS first if enabled
                    if gcs_uploader:
                        dest_blob = f"{task.task_id}/{file_name}"
                        gcs_url = gcs_uploader.upload_file(f_path, dest_blob)
                        if gcs_url:
                            gcs_urls[file_name] = gcs_url
                        else:
                            logging.error(f"GCS upload failed for {f_path}")
                            file_success = False

                    # 3. Upload to Dump Channel
                    if dump_channel and application_bot and loop:
                        try:
                            # If we have userbot, telegram_chunked_upload will use it when possible
                            success = asyncio.run_coroutine_threadsafe(
                                telegram_chunked_upload(
                                    f_path,
                                    context,
                                    chat_id=dump_channel,
                                    reply_to_message_id=None,
                                    userbot_uploader=userbot_uploader
                                ),
                                loop
                            ).result()

                            if not success:
                                file_success = False

                            if success and owner_id:
                                try:
                                    msg = f"✅ Uploaded `{file_name}` ({format_size(os.path.getsize(f_path))})"
                                    asyncio.run_coroutine_threadsafe(
                                        application_bot.send_message(chat_id=owner_id, text=msg, parse_mode="Markdown"),
                                        loop
                                    )
                                except Exception:
                                    pass

                        except Exception as e:
                            logging.error(f"Telegram upload failed for {f_path}: {e}")
                            file_success = False

                    final_status[file_name] = file_success

                    # Mark as successfully uploaded for StorageManager to potentially clean up
                    if file_success:
                        storage_manager.mark_uploaded(f_path)

                # Stop userbot if it was started
                if userbot_uploader:
                    try:
                        asyncio.run_coroutine_threadsafe(userbot_uploader.stop(), loop).result()
                    except Exception as e:
                        logging.error(f"Failed to stop UserbotUploader: {e}")

                # Send Final Summary
                if application_bot and loop:
                    summary_msg = f"🏁 **Task Complete Summary**\n**Task ID:** `{task.task_id}`\n\n"
                    for fname, success in final_status.items():
                        status_icon = "✅" if success else "❌"
                        summary_msg += f"{status_icon} `{fname}`\n"
                        if fname in gcs_urls:
                            summary_msg += f"   └ GCS: `{gcs_urls[fname]}`\n"

                    if dump_channel:
                        try:
                            asyncio.run_coroutine_threadsafe(
                                application_bot.send_message(chat_id=dump_channel, text=summary_msg, reply_to_message_id=dump_metadata_message_id, parse_mode="Markdown"),
                                loop
                            )
                        except Exception as e:
                            logging.error(f"Failed to send summary to dump channel: {e}")

                    if owner_id:
                        try:
                            asyncio.run_coroutine_threadsafe(
                                application_bot.send_message(chat_id=owner_id, text=summary_msg, parse_mode="Markdown"),
                                loop
                            )
                        except Exception as e:
                            logging.error(f"Failed to send summary to owner: {e}")

                queue_mgr.update_task_status(task.task_id, "finished")

        except Exception as e:
            logging.error(f"Worker loop error on task {task.task_id}: {e}")
            task.error = str(e)
            queue_mgr.update_task_status(task.task_id, "failed")
            if application_bot and loop:
                admin_ids = config.get("Bot", "admin_ids", fallback="").split(",")
                for a_id in admin_ids:
                    if a_id.strip():
                        try:
                            msg = f"⚠️ *Critical Worker Error* on task `{task.task_id}`:\n{str(e)}"
                            asyncio.run_coroutine_threadsafe(
                                application_bot.send_message(chat_id=int(a_id.strip()), text=msg, parse_mode="Markdown"),
                                loop
                            )
                        except Exception:
                            pass

async def main():
    bot_token = config.get("Bot", "token", fallback="")
    if not bot_token:
        logging.error("Bot token not found in config.cfg. Exiting.")
        return

    # Start bot application
    application = ApplicationBuilder().token(bot_token).build()
    start_bot(application)

    # Get the current asyncio event loop
    loop = asyncio.get_running_loop()

    # Start worker thread (pass application bot and the event loop for safe notifications)
    worker_thread = threading.Thread(target=worker_loop, args=(application.bot, loop), daemon=True)
    worker_thread.start()

    # Start storage manager background thread
    from storage_manager import StorageManager
    download_dir = config.get("Storage", "download_dir", fallback="./downloads")
    max_local_usage_gb = config.getfloat("Storage", "max_local_usage_gb", fallback=70.0)
    owner_id_str = config.get("Telegram", "owner_id", fallback="")
    owner_id = int(owner_id_str) if owner_id_str else None

    storage_manager = StorageManager(download_dir=download_dir, max_local_usage_gb=max_local_usage_gb)
    storage_thread = threading.Thread(target=storage_manager.run_worker, args=(application, owner_id), daemon=True)
    storage_thread.start()

    # Run FastAPI server
    server_config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    fastapi_server = uvicorn.Server(server_config)

    # application.run_polling() is blocking, so we use application hooks or initialize it manually.
    # The modern PTB way to run alongside another async server is to initialize the app and start polling,
    # then await the other server.
    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    try:
        await fastapi_server.serve()
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Shutting down...")
        queue_mgr.save_state()

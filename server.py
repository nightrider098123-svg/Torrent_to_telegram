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
from utils import setup_logging
from uploader import DriveUploader

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

                # Drive upload logic
                # For `target_folder_id`, re-read config dynamically so /setfolder takes effect immediately
                from utils import load_config
                current_config = load_config()
                target_folder_id = current_config.get("Drive", "target_folder_id", fallback="")

                uploaded_to_drive = False
                if target_folder_id:
                    if os.path.isfile(file_to_upload):
                        drive_id = drive_uploader.upload_file(file_to_upload, target_folder_id, task, progress_callback)
                        if drive_id:
                            logging.info(f"Uploaded file to Drive with ID: {drive_id}")
                            uploaded_to_drive = True
                    elif os.path.isdir(file_to_upload):
                        success = drive_uploader.upload_directory(file_to_upload, target_folder_id, task, progress_callback)
                        if success:
                            logging.info(f"Uploaded directory {file_to_upload} to Drive")
                            uploaded_to_drive = True

                # Telegram upload fallback
                if not uploaded_to_drive and application_bot and loop and task.user_id:
                    from uploader import telegram_chunked_upload

                    logging.info(f"Uploading task {task.task_id} to Telegram as fallback/primary")
                    class MockContext:
                        def __init__(self, bot):
                            self.bot = bot

                    context = MockContext(application_bot)

                    if os.path.isfile(file_to_upload):
                        asyncio.run_coroutine_threadsafe(
                            telegram_chunked_upload(file_to_upload, context, task.user_id, task.message_id),
                            loop
                        ).result() # Wait for completion since we are in a worker thread
                    elif os.path.isdir(file_to_upload):
                        for root, _, files in os.walk(file_to_upload):
                            for file in files:
                                f_path = os.path.join(root, file)
                                asyncio.run_coroutine_threadsafe(
                                    telegram_chunked_upload(f_path, context, task.user_id, task.message_id),
                                    loop
                                ).result()

                # Cleanup task directory after successful upload
                import shutil
                if task.type != "local_upload" and os.path.exists(task_download_dir):
                    try:
                        shutil.rmtree(task_download_dir)
                        logging.info(f"Cleaned up isolated download directory: {task_download_dir}")
                    except Exception as clean_e:
                        logging.warning(f"Failed to clean up {task_download_dir}: {clean_e}")

                queue_mgr.update_task_status(task.task_id, "finished")
                if application_bot and loop and task.user_id:
                    msg = f"✅ Task `{task.task_id}` has finished uploading."
                    asyncio.run_coroutine_threadsafe(
                        application_bot.send_message(chat_id=task.user_id, text=msg, parse_mode="Markdown"),
                        loop
                    )

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

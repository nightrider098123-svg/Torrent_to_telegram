import os
import uuid
import logging
import asyncio
from typing import List
from telegram import Update, Message
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from queue_manager import QueueManager, Task
from utils import load_config, format_size
from downloader import get_downloader
from uploader import DriveUploader, telegram_chunked_upload

# Initialize config and globals
config = load_config()
ADMIN_IDS: List[int] = [int(x.strip()) for x in config.get("Bot", "admin_ids", fallback="").split(",") if x.strip()]
DOWNLOAD_DIR = config.get("Downloader", "download_dir", fallback="./downloads")
MAX_CONCURRENT = config.getint("Downloader", "max_concurrent_downloads", fallback=2)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

queue_mgr = QueueManager(max_concurrent=MAX_CONCURRENT)
downloader = get_downloader()
drive_uploader = None # Initialize lazily if needed

def is_admin(user_id: int) -> bool:
    if not ADMIN_IDS:
        logging.warning("No admin_ids configured. All requests will be rejected.")
        return False
    return user_id in ADMIN_IDS

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.message.from_user.id):
        await update.message.reply_text("Unauthorized. You are not an admin.")
        return
    await update.message.reply_text("Hello! I am a Torrent Downloader Bot. Send me a .torrent file or a magnet link to begin.")

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.message.from_user.id):
        await update.message.reply_text("Unauthorized.")
        return
    help_text = (
        "Commands:\n"
        "/start - Start the bot\n"
        "/download <magnet> - Download a magnet link\n"
        "/upload <local_path> - Upload a local file directly\n"
        "/status - Show current queue and downloads\n"
        "/confirm <id> - Confirm a large download\n"
        "/cancel <id> - Cancel a specific task\n"
        "/list - List all active tasks\n"
        "/setfolder <drive_folder_id> - Set target Google Drive folder\n"
        "/health - Show system health info"
    )
    await update.message.reply_text(help_text)

async def _enqueue_task(update: Update, task_type: str, source: str) -> None:
    task_id = str(uuid.uuid4())[:8]
    task = Task(
        task_id=task_id,
        type=task_type,
        source=source,
        user_id=update.message.from_user.id,
        message_id=update.message.message_id
    )
    queue_mgr.add_task(task)
    await update.message.reply_text(f"Task `{task_id}` added to queue.", parse_mode="Markdown")

async def download_cmd_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.message.from_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /download <magnet_link>")
        return
    magnet_link = context.args[0]
    if not magnet_link.startswith("magnet:?"):
        await update.message.reply_text("Invalid magnet link format.")
        return
    await _enqueue_task(update, "magnet", magnet_link)

async def upload_cmd_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.message.from_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /upload <local_path>")
        return
    local_path = " ".join(context.args)
    if not os.path.exists(local_path):
        await update.message.reply_text(f"File not found: {local_path}")
        return

    # We add this as a task that immediately skips to upload
    task_id = str(uuid.uuid4())[:8]
    task = Task(
        task_id=task_id,
        type="local_upload",
        source=local_path,
        user_id=update.message.from_user.id,
        message_id=update.message.message_id,
        status="completed" # Start immediately from the upload phase
    )
    queue_mgr.add_task(task)
    await update.message.reply_text(f"Upload task `{task_id}` added.", parse_mode="Markdown")

async def confirm_cmd_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.message.from_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /confirm <task_id>")
        return
    task_id = context.args[0]
    task = queue_mgr.get_task(task_id)
    if not task:
        await update.message.reply_text("Task not found.")
        return
    if task.status != "waiting_admin_confirmation":
        await update.message.reply_text(f"Task is in status `{task.status}`, not waiting for confirmation.")
        return

    # Bypass the size limit by setting the approval flag
    task.approved_large_file = True
    queue_mgr.update_task_status(task_id, "queued")
    await update.message.reply_text(f"Task `{task_id}` confirmed and added back to queue.", parse_mode="Markdown")

async def setfolder_cmd_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.message.from_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /setfolder <drive_folder_id>")
        return
    folder_id = context.args[0]
    from utils import rotate_token
    rotate_token("config.cfg", "Drive", "target_folder_id", folder_id)
    await update.message.reply_text(f"Google Drive target folder updated to: `{folder_id}`", parse_mode="Markdown")

async def torrent_file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.message.from_user.id):
        return
    doc = update.message.document
    if doc.file_name.endswith('.torrent'):
        file = await doc.get_file()
        file_path = os.path.join(DOWNLOAD_DIR, doc.file_name)
        await file.download_to_drive(file_path)
        await _enqueue_task(update, "torrent", file_path)

async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.message.from_user.id):
        return
    tasks = queue_mgr.get_all_tasks()
    if not tasks:
        await update.message.reply_text("No active tasks.")
        return

    msg = ""
    for t in tasks:
        msg += f"*ID:* `{t.task_id}`\n"
        msg += f"Status: {t.status}\n"
        if t.status == "downloading":
            msg += f"Progress: {t.progress:.1f}%\n"
            msg += f"Speed: {format_size(t.speed)}/s\n"
            msg += f"ETA: {t.eta}s\n"
        elif t.status == "failed":
            msg += f"Error: {t.error}\n"
        msg += "---\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.message.from_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /cancel <task_id>")
        return
    task_id = context.args[0]
    task = queue_mgr.get_task(task_id)
    if not task:
        await update.message.reply_text("Task not found.")
        return

    if task.status in ["downloading", "uploading"]:
        downloader.cancel(task_id)
        queue_mgr.update_task_status(task_id, "cancelled")
        await update.message.reply_text(f"Task `{task_id}` cancelled.", parse_mode="Markdown")
    else:
        queue_mgr.remove_task(task_id)
        await update.message.reply_text(f"Task `{task_id}` removed from queue.", parse_mode="Markdown")

async def health_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    import shutil
    if not is_admin(update.message.from_user.id):
        return
    total, used, free = shutil.disk_usage("/")
    tasks = queue_mgr.get_all_tasks()
    active_count = sum(1 for t in tasks if t.status in ["downloading", "uploading"])

    health_msg = (
        f"*Disk Space:* {format_size(free)} free of {format_size(total)}\n"
        f"*Active Tasks:* {active_count}/{MAX_CONCURRENT}\n"
        f"*Queued Tasks:* {len([t for t in tasks if t.status == 'queued'])}\n"
    )
    await update.message.reply_text(health_msg, parse_mode="Markdown")

def start_bot(application: Application):
    """Adds handlers and runs bot."""
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("help", help_handler))
    application.add_handler(CommandHandler("download", download_cmd_handler))
    application.add_handler(CommandHandler("upload", upload_cmd_handler))
    application.add_handler(CommandHandler("setfolder", setfolder_cmd_handler))
    application.add_handler(CommandHandler("confirm", confirm_cmd_handler))
    application.add_handler(CommandHandler("status", status_handler))
    application.add_handler(CommandHandler("list", status_handler))
    application.add_handler(CommandHandler("cancel", cancel_handler))
    application.add_handler(CommandHandler("health", health_handler))
    application.add_handler(MessageHandler(filters.Document.ALL, torrent_file_handler))

# // Telegram message listener for bot commands, integrating the pipeline
import asyncio
from pyrogram import Client, filters, idle
from pyrogram.types import Message
import logging
from bot.config import Config, load_config, setup_logging
from bot.download_manager import DownloadManager
from bot.upload_queue import UploadQueue, UploadTask
from bot.uploader_worker import UploaderWorker
from bot.disk_safety_monitor import DiskSafetyMonitor
from bot.state_persistence import StatePersistence
import os

logger = logging.getLogger(__name__)

class BotApp:
    def __init__(self, config: Config):
        self.config = config

        # Prefer user session if string is provided
        if self.config.USER_SESSION_STRING:
            logger.info("Using MTProto User Session")
            self.client = Client("userbot", session_string=self.config.USER_SESSION_STRING,
                                 api_id=self.config.API_ID, api_hash=self.config.API_HASH)
        elif self.config.BOT_TOKEN:
            logger.info("Using Telegram Bot API")
            self.client = Client("bot", bot_token=self.config.BOT_TOKEN,
                                 api_id=self.config.API_ID, api_hash=self.config.API_HASH)
        else:
            raise ValueError("Must provide either USER_SESSION_STRING or BOT_TOKEN")

        self.download_manager = DownloadManager(self.config.DOWNLOAD_DIR)
        self.queue = UploadQueue()
        self.state_persistence = StatePersistence(self.client, self.config.ADMIN_CHANNEL_ID)
        self.upload_allowed = asyncio.Event()
        self.upload_allowed.set() # Default to True unless BATCH_MODE is true and disk is not full

        self.uploader_workers = [UploaderWorker(self.client, self.queue, self.config.ADMIN_CHANNEL_ID, self.config.SPLIT_SIZE_BYTES, self.state_persistence, self.upload_allowed) for _ in range(self.config.UPLOAD_WORKERS)]
        self.disk_monitor = DiskSafetyMonitor(self.download_manager, self.client, self.config.ADMIN_CHANNEL_ID,
                                              self.config.DOWNLOAD_DIR, self.config.MAX_DISK_USED_GB, self.config.RESUME_DISK_USED_GB,
                                              self.config.BATCH_MODE, self.upload_allowed)

    async def _on_file_complete(self, gid: str, file_path: str):
        logger.info(f"File complete event received for {gid} at {file_path}")
        size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
        task = UploadTask(
            file_path=file_path,
            dest_channel=self.config.DUMP_CHANNEL_ID,
            original_filename=os.path.basename(file_path),
            size_bytes=size,
            start_part_num=1
        )
        await self.queue.enqueue(task)
        await self.state_persistence.update_uploading(file_path)

    async def _on_download_complete(self, gid: str):
        logger.info(f"Entire download complete event received for {gid}")
        # Find and remove the magnet associated with this gid from state
        state = await self.state_persistence.load_state()
        magnets_to_remove = []
        for m in state.get("queued_magnets", []):
            if isinstance(m, dict) and m.get("gid") == gid:
                magnets_to_remove.append(m)

        for m in magnets_to_remove:
            state["queued_magnets"].remove(m)

        if magnets_to_remove:
            await self.state_persistence.save_state(state)

    def _setup_handlers(self):
        @self.client.on_message(filters.chat(self.config.ADMIN_CHANNEL_ID) & filters.text)
        async def handle_admin_commands(client: Client, message: Message):
            text = message.text.strip().split()
            cmd = text[0].upper()
            args = text[1:]

            try:
                if cmd == "ADD":
                    if not args:
                        await message.reply("Usage: ADD <magnet_link>")
                        return
                    gid = await self.download_manager.add_torrent(args[0])
                    if gid:
                        await message.reply(f"Added download with ID: {gid}")

                        # Save state
                        state = await self.state_persistence.load_state()
                        # Store as dict with gid
                        magnet_entry = {"url": args[0], "gid": gid}
                        if magnet_entry not in state["queued_magnets"]:
                            state["queued_magnets"].append(magnet_entry)
                            await self.state_persistence.save_state(state)
                    else:
                        await message.reply("Failed to add download.")

                elif cmd == "PAUSE":
                    await self.download_manager.pause_all()
                    await message.reply("All downloads paused.")

                elif cmd == "RESUME":
                    await self.download_manager.resume()
                    await message.reply("Downloads resumed.")

                elif cmd == "STATUS":
                    import json
                    used_gb = self.disk_monitor.get_used_gb()
                    status = await self.download_manager.get_status()
                    download_speed = int(status.get("downloadSpeed", 0)) if status else 0

                    queue_size = self.queue._queue.qsize()
                    state_cache = self.state_persistence._state_cache
                    uploading_count = len(state_cache.get("uploading", []))

                    reply_data = {
                        "download_speed_bytes": download_speed,
                        "queued_files_count": queue_size,
                        "uploading_count": uploading_count,
                        "disk_used_gb": round(used_gb, 2)
                    }
                    await message.reply(f"```json\n{json.dumps(reply_data, indent=2)}\n```")

                elif cmd == "SHUTDOWN":
                    await message.reply("Shutting down bot...")
                    # Allow gracefully stopping
                    asyncio.create_task(self.stop())

                elif cmd == "SET" and len(args) == 2 and args[0].upper() == "MAX_GB":
                    new_max = float(args[1])
                    self.config.MAX_DISK_USED_GB = new_max
                    self.disk_monitor.max_gb = new_max
                    await message.reply(f"Set MAX_DISK_USED_GB to {new_max} GB")

            except Exception as e:
                logger.error(f"Error handling admin command: {e}")
                await message.reply(f"Error: {e}")

    async def start(self):
        logger.info("Starting bot app...")

        await self.client.start()
        logger.info("Telegram client started.")

        if self.config.BATCH_MODE:
             self.upload_allowed.clear() # Only allow uploads when disk gets full
             logger.info("Batch mode enabled. Uploads paused until disk threshold.")

        # Load state
        state = await self.state_persistence.load_state()
        logger.info(f"Loaded state with {len(state.get('queued_magnets', []))} queued magnets")

        # Re-add queued magnets if they aren't in aria2 already
        status = await self.download_manager.get_status()
        num_active = int(status.get("numActive", 0)) if status else 0
        num_waiting = int(status.get("numWaiting", 0)) if status else 0
        if num_active == 0 and num_waiting == 0:
            # We must map old strings to dicts for backwards compatibility if applicable
            new_magnets_list = []
            for magnet in state.get("queued_magnets", []):
                url = magnet.get("url") if isinstance(magnet, dict) else magnet
                logger.info(f"Recovering download: {url}")
                new_gid = await self.download_manager.add_torrent(url)
                if new_gid:
                    new_magnets_list.append({"url": url, "gid": new_gid})
            state["queued_magnets"] = new_magnets_list
            await self.state_persistence.save_state(state)

        # Look for partially uploaded/completed files in download dir and subdirectories
        if os.path.exists(self.config.DOWNLOAD_DIR):
            for root, _, files in os.walk(self.config.DOWNLOAD_DIR):
                for file in files:
                    full_path = os.path.join(root, file)
                    if os.path.isfile(full_path):
                        if full_path in state.get("uploading", []):
                            logger.info(f"Recovering completed file: {full_path}")
                            await self._on_file_complete("recovered", full_path)

        self.download_manager.register_on_file_complete(self._on_file_complete)
        self.download_manager.register_on_download_complete(self._on_download_complete)
        await self.download_manager.start()

        for worker in self.uploader_workers:
            asyncio.create_task(worker.start())

        await self.disk_monitor.start()

        self._setup_handlers()

        msg = "Bot started successfully. Waiting for commands."
        await self.client.send_message(self.config.ADMIN_CHANNEL_ID, msg)

        await idle()
        await self.stop()

    async def stop(self):
        logger.info("Stopping bot app...")
        await self.disk_monitor.stop()
        for worker in self.uploader_workers:
            await worker.stop()
        await self.download_manager.stop()
        await self.client.stop()
        logger.info("Bot app stopped.")

def main():
    config = load_config()
    setup_logging(config.LOG_FILE)

    app = BotApp(config)
    asyncio.run(app.start())

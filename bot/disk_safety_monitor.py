# // Monitors disk usage and triggers pause/resume on download manager
import psutil
import asyncio
import logging
from bot.download_manager import DownloadManager
from pyrogram import Client

logger = logging.getLogger(__name__)

class DiskSafetyMonitor:
    def __init__(self, download_manager: DownloadManager, client: Client, admin_channel_id: int,
                 download_dir: str, max_gb: float, resume_gb: float, batch_mode: bool = False,
                 upload_allowed: asyncio.Event = None, poll_interval: int = 30):
        self.download_manager = download_manager
        self.client = client
        self.admin_channel_id = admin_channel_id
        self.download_dir = download_dir
        self.max_gb = max_gb
        self.resume_gb = resume_gb
        self.batch_mode = batch_mode
        self.upload_allowed = upload_allowed or asyncio.Event()
        if not upload_allowed:
            self.upload_allowed.set()

        self.poll_interval = poll_interval
        self._is_paused = False
        self._task = None

    async def start(self):
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info(f"Disk Safety Monitor started. Max: {self.max_gb}GB, Resume: {self.resume_gb}GB, Batch Mode: {self.batch_mode}")

    async def stop(self):
        if self._task:
            self._task.cancel()
        logger.info("Disk Safety Monitor stopped.")

    async def _monitor_loop(self):
        while True:
            try:
                await self.check_disk()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Disk monitor error: {e}")
            await asyncio.sleep(self.poll_interval)

    def get_used_gb(self) -> float:
        try:
            usage = psutil.disk_usage(self.download_dir)
            return usage.used / (1024**3)
        except Exception as e:
            logger.error(f"Failed to get disk usage: {e}")
            return 0.0

    async def check_disk(self):
        used_gb = self.get_used_gb()

        if used_gb >= self.max_gb and not self._is_paused:
            self._is_paused = True
            await self.download_manager.pause_new_downloads()
            await self.download_manager.pause_all()

            # If batch mode, trigger uploads now
            if self.batch_mode:
                self.upload_allowed.set()
                logger.info("Batch Mode: Disk threshold reached. Enabling uploads.")

            msg = f"Disk usage high: {used_gb:.2f} GB — pausing new downloads; flushing upload queue."
            logger.warning(msg)
            try:
                await self.client.send_message(self.admin_channel_id, msg)
            except Exception as e:
                logger.error(f"Failed to send disk pause alert: {e}")

        elif used_gb <= self.resume_gb and self._is_paused:
            self._is_paused = False
            await self.download_manager.resume()

            # If batch mode, stop uploads until next batch
            if self.batch_mode:
                self.upload_allowed.clear()
                logger.info("Batch Mode: Disk usage reduced. Disabling uploads.")

            msg = f"Disk usage reduced: {used_gb:.2f} GB — resuming downloads."
            logger.info(msg)
            try:
                await self.client.send_message(self.admin_channel_id, msg)
            except Exception as e:
                logger.error(f"Failed to send disk resume alert: {e}")

        # Emergency GC if near 95% total disk usage
        try:
            total = psutil.disk_usage(self.download_dir).total
            percent = (used_gb * (1024**3)) / total * 100
            if percent >= 95:
                logger.critical(f"Disk usage critically high ({percent:.1f}%). Stopping all downloads immediately.")
                await self.download_manager.pause_all()
                self.upload_allowed.set() # Force uploading

                # We normally delete files immediately upon successful upload.
                # If there are stale parts or un-tracked files consuming space, attempt an emergency GC of .part files
                # or old completed files not currently in queue.
                import os
                for root, dirs, files in os.walk(self.download_dir):
                    for file in files:
                        if ".part" in file:
                            try:
                                path = os.path.join(root, file)
                                os.remove(path)
                                logger.warning(f"Emergency GC: deleted leftover part file {path}")
                            except Exception as ex:
                                pass
        except Exception as e:
            logger.error(f"Failed to run emergency GC: {e}")

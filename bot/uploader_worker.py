# // Upload workers for concurrency, Pyrogram client for MTProto uploads, splitting and retrying
import asyncio
import logging
import os
import time
from typing import Optional, List
from pyrogram import Client, errors
import aiofiles
from bot.upload_queue import UploadQueue, UploadTask
from bot.state_persistence import StatePersistence

logger = logging.getLogger(__name__)

class UploaderWorker:
    def __init__(self, client: Client, queue: UploadQueue, admin_channel_id: int, split_size_bytes: int, state_persistence: StatePersistence, upload_allowed: asyncio.Event = None):
        self.client = client
        self.queue = queue
        self.admin_channel_id = admin_channel_id
        self.split_size_bytes = split_size_bytes
        self.state_persistence = state_persistence
        self.upload_allowed = upload_allowed or asyncio.Event()
        if not upload_allowed:
            self.upload_allowed.set()
        self.is_running = False

    async def start(self):
        self.is_running = True
        while self.is_running:
            try:
                task = await self.queue.dequeue()
                # Wait until uploads are allowed (for BATCH_MODE fallback)
                await self.upload_allowed.wait()
                await self.process_task(task)
                self.queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Uploader worker error: {e}")

    async def stop(self):
        self.is_running = False

    async def progress_callback(self, current: int, total: int, file_path: str, task_id: str):
        # Only log every 10% to avoid spam
        progress_pct = (current / total) * 100 if total > 0 else 0

        # We can store last logged progress in a dictionary to throttle it, but for simplicity
        # we log at ~10% intervals using a rough check
        # Instead of state dictionary, just log when current is divisible roughly by 10%
        # Actually Pyrogram calls this very frequently, so we will throttle by time or percentage

        if not hasattr(self, '_last_progress_pct'):
            self._last_progress_pct = {}

        last_pct = self._last_progress_pct.get(file_path, 0)

        if progress_pct - last_pct >= 10 or progress_pct >= 100:
            self._last_progress_pct[file_path] = progress_pct
            logger.info(f"Upload Progress: {os.path.basename(file_path)} {progress_pct:.1f}%", extra={
                "event_type": "upload_progress",
                "id": task_id,
                "filename": os.path.basename(file_path),
                "size": total,
                "progress": progress_pct
            })

        if progress_pct >= 100 and file_path in self._last_progress_pct:
            del self._last_progress_pct[file_path]

    async def _upload_part(self, task: UploadTask, file_path: str, retries: int = 5):
        base_delay = 10
        for attempt in range(retries):
            try:
                logger.info(f"Uploading {file_path} to {task.dest_channel}", extra={
                    "event_type": "upload_started",
                    "id": task.original_filename,
                    "filename": os.path.basename(file_path),
                    "size": os.path.getsize(file_path) if os.path.exists(file_path) else 0,
                    "progress": 0.0
                })

                # Send file
                msg = await self.client.send_document(
                    chat_id=task.dest_channel,
                    document=file_path,
                    file_name=os.path.basename(file_path),
                    caption=os.path.basename(file_path),
                    progress=self.progress_callback,
                    progress_args=(file_path, task.original_filename)
                )
                if msg:
                    return True
            except errors.FloodWait as e:
                wait_time = e.value + (e.value * 0.1)
                logger.warning(f"FloodWait encountered. Waiting {wait_time} seconds.", extra={
                    "event_type": "upload_error",
                    "error": f"FloodWait {e.value}s"
                })
                await asyncio.sleep(wait_time)
            except Exception as e:
                logger.error(f"Upload error on attempt {attempt+1}: {e}", extra={
                    "event_type": "upload_error",
                    "error": str(e)
                })
                await asyncio.sleep(base_delay * (2 ** attempt))

        return False

    async def process_task(self, task: UploadTask):
        try:
            logger.info(f"Processing upload task: {task.original_filename}")
            if not os.path.exists(task.file_path):
                logger.error(f"File not found: {task.file_path}")
                await self.state_persistence.mark_uploaded(task.file_path)
                return

            file_size = os.path.getsize(task.file_path)

            if file_size > self.split_size_bytes:
                logger.info(f"File exceeds split size, splitting: {task.file_path}")

                part_num = task.start_part_num
                success = True
                start_time = time.time()

                # Split the file using aiofiles to avoid blocking the event loop
                # and read in small 5MB chunks to avoid OOM
                chunk_size = 5 * 1024 * 1024

                async with aiofiles.open(task.file_path, "rb") as f:
                    if part_num > 1:
                        await f.seek(self.split_size_bytes * (part_num - 1))

                    while True:
                        part_path = f"{task.file_path}.part{part_num:03d}"
                        bytes_written_this_part = 0

                        # We need to read exactly what's left, or chunk_size
                        remaining = self.split_size_bytes - bytes_written_this_part
                        to_read = min(chunk_size, remaining)

                        # Read the first chunk to see if we reached EOF
                        first_chunk = await f.read(to_read)
                        if not first_chunk:
                            break

                        # Open the part file and write chunks up to split_size_bytes
                        async with aiofiles.open(part_path, "wb") as pf:
                            await pf.write(first_chunk)
                            bytes_written_this_part += len(first_chunk)

                            while bytes_written_this_part < self.split_size_bytes:
                                # We need to read exactly what's left, or chunk_size
                                remaining = self.split_size_bytes - bytes_written_this_part
                                to_read = min(chunk_size, remaining)
                                chunk = await f.read(to_read)
                                if not chunk:
                                    break
                                await pf.write(chunk)
                                bytes_written_this_part += len(chunk)

                        # Upload
                        uploaded = await self._upload_part(task, part_path)

                        # Clean up part
                        if os.path.exists(part_path):
                            os.remove(part_path)

                        if not uploaded:
                            logger.error(f"Failed to upload part {part_path}. Requeueing remaining parts.")
                            success = False
                            # Requeue remaining parts
                            task.start_part_num = part_num
                            await self.queue.enqueue(task)
                            break

                        part_num += 1

                if success:
                    duration = time.time() - start_time
                    await self._notify_admin_success(task, part_num - 1, duration)
                    self._cleanup_original(task.file_path)
                    await self.state_persistence.mark_uploaded(task.file_path)
            else:
                start_time = time.time()
                uploaded = await self._upload_part(task, task.file_path)
                if uploaded:
                    duration = time.time() - start_time
                    await self._notify_admin_success(task, 1, duration)
                    self._cleanup_original(task.file_path)
                    await self.state_persistence.mark_uploaded(task.file_path)
                else:
                    await self._notify_admin_failure(task, "Upload failed after retries.")
                    # Requeue on full failure so it's not forgotten
                    await self.queue.enqueue(task)

        except Exception as e:
            logger.error(f"Error processing task {task.file_path}: {e}")
            await self._notify_admin_failure(task, str(e))
            # Requeue on generic failure so it's not forgotten
            await self.queue.enqueue(task)

    def _cleanup_original(self, file_path: str):
        if os.path.exists(file_path):
            os.remove(file_path)
            logger.info(f"Deleted original file: {file_path}")

    async def _notify_admin_success(self, task: UploadTask, parts: int, duration: float):
        mins, secs = divmod(int(duration), 60)
        hours, mins = divmod(mins, 60)
        time_str = f"{hours:02d}:{mins:02d}:{secs:02d}"

        size_gb = task.size_bytes / (1024**3)
        msg = f"Uploaded: {task.original_filename} ({size_gb:.2f} GB) to @dumpchannel — parts: {parts} — time: {time_str}"
        try:
            await self.client.send_message(self.admin_channel_id, msg)
            logger.info("Sent success notification to admin channel", extra={
                "event_type": "upload_finished",
                "filename": task.original_filename,
                "size": task.size_bytes,
                "parts": parts,
                "duration": duration
            })
        except Exception as e:
            logger.error(f"Failed to send admin success message: {e}")

    async def _notify_admin_failure(self, task: UploadTask, error: str):
        msg = f"Failed to upload: {task.original_filename}\nError: {error}"
        try:
            await self.client.send_message(self.admin_channel_id, msg)
        except Exception as e:
            logger.error(f"Failed to send admin failure message: {e}")

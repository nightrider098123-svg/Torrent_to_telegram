import os
import asyncio
import logging
import math
from typing import Optional, Callable
from pyrogram import Client
from pyrogram.errors import FloodWait, RPCError

from queue_manager import Task

class UserbotUploader:
    def __init__(self, session_string: str, api_id: int, api_hash: str, dump_channel: int, max_retries: int = 5, telegram_file_limit_bytes: int = 2147483648):
        self.session_string = session_string
        self.api_id = api_id
        self.api_hash = api_hash
        self.dump_channel = dump_channel
        self.max_retries = max_retries
        self.telegram_file_limit_bytes = telegram_file_limit_bytes
        self.client: Optional[Client] = None

    async def start(self):
        if not self.client:
            self.client = Client(
                "userbot_uploader",
                session_string=self.session_string,
                api_id=self.api_id,
                api_hash=self.api_hash
            )
            await self.client.start()
            logging.info("Userbot started successfully.")

    async def stop(self):
        if self.client:
            await self.client.stop()
            self.client = None
            logging.info("Userbot stopped successfully.")

    async def upload_file(self, file_path: str, progress_callback: Callable[[int, int], None] = None) -> bool:
        """Uploads a file using the MTProto client with retries and progress updates."""
        if not self.client:
            await self.start()

        if not os.path.exists(file_path):
            logging.error(f"File not found for MTProto upload: {file_path}")
            return False

        file_size = os.path.getsize(file_path)

        for attempt in range(1, self.max_retries + 1):
            try:
                logging.info(f"Uploading via MTProto (attempt {attempt}/{self.max_retries}): {file_path}")

                async def progress(current, total):
                    if progress_callback:
                        progress_callback(current, total)

                # we send document
                await self.client.send_document(
                    chat_id=self.dump_channel,
                    document=file_path,
                    caption=f"{os.path.basename(file_path)}",
                    progress=progress
                )
                logging.info(f"Successfully uploaded via MTProto: {file_path}")
                return True

            except FloodWait as e:
                wait_time = e.value
                logging.warning(f"FloodWait error during MTProto upload. Waiting {wait_time} seconds before retry.")
                await asyncio.sleep(wait_time)
            except RPCError as e:
                logging.error(f"RPCError during MTProto upload: {e}")
                if attempt == self.max_retries:
                    return False
                await asyncio.sleep(5)
            except Exception as e:
                logging.error(f"Unexpected error during MTProto upload: {e}")
                if attempt == self.max_retries:
                    return False
                await asyncio.sleep(5)

        return False

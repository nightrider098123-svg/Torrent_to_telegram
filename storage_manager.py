import os
import json
import logging
import time
import shutil
from typing import List

class StorageManager:
    def __init__(self, download_dir: str, max_local_usage_gb: float, check_interval_sec: int = 60 * 10):
        self.download_dir = download_dir
        self.max_local_usage_bytes = max_local_usage_gb * 1024 * 1024 * 1024
        self.check_interval_sec = check_interval_sec
        self.metadata_file = os.path.join(download_dir, ".uploaded_files_metadata.json")
        self._ensure_metadata_file()

    def _ensure_metadata_file(self):
        if not os.path.exists(self.metadata_file):
            with open(self.metadata_file, "w") as f:
                json.dump({}, f)

    def _load_metadata(self) -> dict:
        try:
            with open(self.metadata_file, "r") as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Failed to load storage metadata: {e}")
            return {}

    def _save_metadata(self, metadata: dict):
        try:
            with open(self.metadata_file, "w") as f:
                json.dump(metadata, f, indent=4)
        except Exception as e:
            logging.error(f"Failed to save storage metadata: {e}")

    def mark_uploaded(self, file_path: str):
        """Marks a file as successfully uploaded so it's eligible for cleanup."""
        if not os.path.abspath(file_path).startswith(os.path.abspath(self.download_dir)):
            return # Only manage files inside download_dir

        metadata = self._load_metadata()
        metadata[file_path] = time.time() # Store completion timestamp
        self._save_metadata(metadata)

    def _get_dir_size(self, path='.'):
        total = 0
        for entry in os.scandir(path):
            if entry.is_file():
                if entry.name != os.path.basename(self.metadata_file):
                    total += entry.stat().st_size
            elif entry.is_dir():
                total += self._get_dir_size(entry.path)
        return total

    def check_and_clean(self, bot_application=None, owner_id=None):
        """Checks total disk usage and deletes oldest uploaded files if necessary."""
        if not os.path.exists(self.download_dir):
            return

        total_size_bytes = self._get_dir_size(self.download_dir)

        if total_size_bytes <= self.max_local_usage_bytes:
            return

        logging.info(f"Local storage usage ({total_size_bytes} bytes) exceeds limit ({self.max_local_usage_bytes} bytes). Initiating cleanup.")

        metadata = self._load_metadata()
        if not metadata:
            logging.warning("Storage limit exceeded but no files are marked as uploaded to delete.")
            return

        # Sort files by upload time (oldest first)
        uploaded_files = sorted(metadata.items(), key=lambda item: item[1])

        freed_bytes = 0
        deleted_files_count = 0

        for file_path, upload_time in uploaded_files:
            if total_size_bytes - freed_bytes <= self.max_local_usage_bytes:
                break # Limit reached

            if os.path.exists(file_path):
                try:
                    size = os.path.getsize(file_path)
                    os.remove(file_path)
                    freed_bytes += size
                    deleted_files_count += 1
                    logging.info(f"Deleted uploaded file to free space: {file_path}")
                except Exception as e:
                    logging.error(f"Failed to delete {file_path}: {e}")

            # Remove from metadata whether delete succeeded or file didn't exist
            del metadata[file_path]

        self._save_metadata(metadata)

        if freed_bytes > 0:
            freed_gb = freed_bytes / (1024 * 1024 * 1024)
            msg = f"🧹 Storage cleanup performed: freed {freed_gb:.2f} GB from {deleted_files_count} files."
            logging.info(msg)

            # Optionally send to owner
            if bot_application and owner_id:
                import asyncio
                try:
                    loop = asyncio.get_running_loop()
                    asyncio.run_coroutine_threadsafe(
                        bot_application.bot.send_message(chat_id=owner_id, text=msg),
                        loop
                    )
                except Exception as e:
                    logging.error(f"Failed to send cleanup notification to owner: {e}")

    def run_worker(self, bot_application=None, owner_id=None):
        """Infinite loop to periodically check storage. Runs in a thread."""
        logging.info("Started Storage Manager worker.")
        while True:
            try:
                self.check_and_clean(bot_application, owner_id)
            except Exception as e:
                logging.error(f"Error in Storage Manager worker: {e}")
            time.sleep(self.check_interval_sec)

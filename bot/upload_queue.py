# // Core upload queue, storing task metadata safely for async consumption
import asyncio
from typing import Dict, Any, List
import logging
from dataclasses import dataclass
import os

logger = logging.getLogger(__name__)

@dataclass
class UploadTask:
    file_path: str
    dest_channel: int
    original_filename: str
    size_bytes: int
    status: str = "queued"
    start_part_num: int = 1

class UploadQueue:
    def __init__(self):
        self._queue = asyncio.Queue()
        self._items: Dict[str, UploadTask] = {}

    async def enqueue(self, task: UploadTask):
        self._items[task.file_path] = task
        await self._queue.put(task)
        logger.info(f"Task enqueued: {task.original_filename}")

    async def dequeue(self) -> UploadTask:
        return await self._queue.get()

    def task_done(self):
        self._queue.task_done()

    def get_snapshot(self) -> List[Dict[str, Any]]:
        return [
            {
                "file_path": v.file_path,
                "dest_channel": v.dest_channel,
                "original_filename": v.original_filename,
                "size_bytes": v.size_bytes,
                "status": v.status,
                "start_part_num": v.start_part_num
            } for v in self._items.values()
        ]

    def remove_task(self, file_path: str):
        if file_path in self._items:
            del self._items[file_path]
            logger.info(f"Task removed from state tracking: {file_path}")

import json
import logging
import os
import time
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, asdict

@dataclass
class Task:
    task_id: str
    type: str # "magnet" or "torrent"
    source: str # magnet link or path to torrent file
    priority: int = 0
    status: str = "queued" # queued, downloading, uploading, completed, failed, cancelled
    user_id: int = 0
    message_id: int = 0
    progress: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed: int = 0
    eta: int = 0
    peers: int = 0
    error: str = ""
    added_at: float = 0.0
    approved_large_file: bool = False

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Task":
        return cls(**data)

class QueueManager:
    def __init__(self, state_file: str = "queue_state.json", max_concurrent: int = 2):
        self.state_file = state_file
        self.max_concurrent = max_concurrent
        self.tasks: Dict[str, Task] = {}
        self._load_state()

    def _load_state(self) -> None:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    data = json.load(f)
                    for task_id, task_data in data.items():
                        task = Task.from_dict(task_data)
                        # Reset transient states
                        if task.status in ["downloading", "uploading"]:
                            task.status = "queued"
                        self.tasks[task_id] = task
                logging.info(f"Loaded {len(self.tasks)} tasks from state file.")
            except Exception as e:
                logging.error(f"Failed to load queue state: {e}")

    def save_state(self) -> None:
        try:
            with open(self.state_file, "w") as f:
                data = {task_id: asdict(task) for task_id, task in self.tasks.items()}
                json.dump(data, f, indent=4)
            # logging.debug("Saved queue state.")
        except Exception as e:
            logging.error(f"Failed to save queue state: {e}")

    def add_task(self, task: Task) -> None:
        if task.added_at == 0.0:
            task.added_at = time.time()
        self.tasks[task.task_id] = task
        self.save_state()
        logging.info(f"Added task {task.task_id} to queue.")

    def get_task(self, task_id: str) -> Optional[Task]:
        return self.tasks.get(task_id)

    def get_next_task(self) -> Optional[Task]:
        # Count active tasks
        active_count = sum(1 for task in self.tasks.values() if task.status in ["downloading", "uploading"])
        if active_count >= self.max_concurrent:
            return None

        # Sort queued tasks by priority (descending) and then by added_at (ascending)
        queued_tasks = [t for t in self.tasks.values() if t.status == "queued"]
        if not queued_tasks:
            return None

        queued_tasks.sort(key=lambda t: (-t.priority, t.added_at))
        task = queued_tasks[0]
        return task

    def update_task_status(self, task_id: str, status: str) -> None:
        if task_id in self.tasks:
            self.tasks[task_id].status = status
            self.save_state()

    def remove_task(self, task_id: str) -> None:
        if task_id in self.tasks:
            del self.tasks[task_id]
            self.save_state()

    def get_all_tasks(self) -> List[Task]:
        return list(self.tasks.values())

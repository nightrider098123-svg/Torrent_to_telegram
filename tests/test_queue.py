import os
import json
import tempfile
import time
from queue_manager import QueueManager, Task

def test_queue_enqueue_dequeue():
    with tempfile.NamedTemporaryFile(delete=False) as f:
        state_file = f.name

    qm = QueueManager(state_file=state_file, max_concurrent=2)

    t1 = Task(task_id="t1", type="magnet", source="link1", priority=1)
    t2 = Task(task_id="t2", type="magnet", source="link2", priority=5)

    qm.add_task(t1)
    qm.add_task(t2)

    # Highest priority should come first
    next_task = qm.get_next_task()
    assert next_task.task_id == "t2"

    # Mark as downloading to "consume" a slot
    qm.update_task_status(next_task.task_id, "downloading")

    # Next task should be t1
    next_task_2 = qm.get_next_task()
    assert next_task_2.task_id == "t1"
    qm.update_task_status(next_task_2.task_id, "downloading")

    # No more slots (max_concurrent=2)
    t3 = Task(task_id="t3", type="magnet", source="link3", priority=10)
    qm.add_task(t3)

    assert qm.get_next_task() is None

    # Free a slot
    qm.update_task_status("t1", "completed")

    # Now t3 should be available
    next_task_3 = qm.get_next_task()
    assert next_task_3.task_id == "t3"

    os.remove(state_file)

def test_queue_persistence():
    with tempfile.NamedTemporaryFile(delete=False) as f:
        state_file = f.name

    qm1 = QueueManager(state_file=state_file, max_concurrent=2)
    t1 = Task(task_id="p1", type="magnet", source="link1")
    qm1.add_task(t1)

    # Create new instance to test loading state
    qm2 = QueueManager(state_file=state_file, max_concurrent=2)
    loaded_task = qm2.get_task("p1")

    assert loaded_task is not None
    assert loaded_task.source == "link1"
    assert loaded_task.status == "queued" # transient state reset not applicable since it was queued initially

    # Test transient state reset
    qm2.update_task_status("p1", "downloading")
    qm3 = QueueManager(state_file=state_file, max_concurrent=2)
    reloaded_task = qm3.get_task("p1")

    # "downloading" should be reset to "queued" on load
    assert reloaded_task.status == "queued"

    os.remove(state_file)

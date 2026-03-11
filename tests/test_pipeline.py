# // Pytest suite to test pipeline steps (download completion, queueing, disk safety, uploader handling).
import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from bot.download_manager import DownloadManager
from bot.upload_queue import UploadQueue, UploadTask
from bot.uploader_worker import UploaderWorker
from bot.disk_safety_monitor import DiskSafetyMonitor
from bot.state_persistence import StatePersistence
from pyrogram.errors import FloodWait

@pytest.fixture
def mock_client():
    client = MagicMock()
    client.send_document = AsyncMock(return_value=True)
    client.send_message = AsyncMock()
    return client

@pytest.fixture
def state_persistence():
    state = MagicMock(spec=StatePersistence)
    state.mark_uploaded = AsyncMock()
    state.update_uploading = AsyncMock()
    return state

@pytest.fixture
def download_manager():
    return DownloadManager(download_dir="/tmp/downloads")

@pytest.fixture
def upload_queue():
    return UploadQueue()

@pytest.mark.asyncio
async def test_download_manager_queueing(download_manager):
    # Mock RPC call
    download_manager._call_rpc = AsyncMock(return_value="gid123")

    gid = await download_manager.add_torrent("magnet:?xt=urn:btih:test")
    assert gid == "gid123"
    assert "gid123" in download_manager.downloads

@pytest.mark.asyncio
async def test_upload_queue():
    queue = UploadQueue()
    task = UploadTask(
        file_path="/tmp/test.mkv",
        dest_channel=123,
        original_filename="test.mkv",
        size_bytes=100
    )

    await queue.enqueue(task)
    assert queue._queue.qsize() == 1

    dequeued = await queue.dequeue()
    assert dequeued.original_filename == "test.mkv"
    assert dequeued.start_part_num == 1

@pytest.mark.asyncio
async def test_uploader_worker_success(mock_client, upload_queue, tmp_path, state_persistence):
    worker = UploaderWorker(mock_client, upload_queue, admin_channel_id=123, split_size_bytes=1000, state_persistence=state_persistence)

    # Create dummy file
    test_file = tmp_path / "test.mkv"
    test_file.write_bytes(b"A" * 500)  # 500 bytes, won't split

    task = UploadTask(
        file_path=str(test_file),
        dest_channel=123,
        original_filename="test.mkv",
        size_bytes=500
    )

    await worker.process_task(task)

    # Verify upload was called
    mock_client.send_document.assert_called_once()
    # Verify success message sent to admin
    mock_client.send_message.assert_called_once()
    assert "Uploaded: test.mkv" in mock_client.send_message.call_args[0][1]
    # Verify file was deleted
    assert not test_file.exists()
    # Verify marked uploaded in state
    state_persistence.mark_uploaded.assert_called_once_with(str(test_file))

@pytest.mark.asyncio
async def test_uploader_worker_split(mock_client, upload_queue, tmp_path, state_persistence):
    # Test splitting logic
    worker = UploaderWorker(mock_client, upload_queue, admin_channel_id=123, split_size_bytes=200, state_persistence=state_persistence)

    # Create 500 byte file, should split into 3 parts (200, 200, 100)
    test_file = tmp_path / "big_test.mkv"
    test_file.write_bytes(b"A" * 500)

    task = UploadTask(
        file_path=str(test_file),
        dest_channel=123,
        original_filename="big_test.mkv",
        size_bytes=500
    )

    await worker.process_task(task)

    # Should be called 3 times
    assert mock_client.send_document.call_count == 3
    # Original file deleted
    assert not test_file.exists()
    # Ensure parts are deleted
    assert not (tmp_path / "big_test.mkv.part001").exists()

@pytest.mark.asyncio
async def test_uploader_worker_floodwait(mock_client, upload_queue, tmp_path, state_persistence):
    worker = UploaderWorker(mock_client, upload_queue, admin_channel_id=123, split_size_bytes=1000, state_persistence=state_persistence)

    test_file = tmp_path / "test2.mkv"
    test_file.write_bytes(b"A" * 10)

    task = UploadTask(
        file_path=str(test_file),
        dest_channel=123,
        original_filename="test2.mkv",
        size_bytes=10
    )

    # Mock flood wait on first try, success on second
    mock_client.send_document = AsyncMock(side_effect=[FloodWait(value=1), True])

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await worker.process_task(task)

        # Sleep called for flood wait
        mock_sleep.assert_called_once()
        # Verify it slept for wait time + 10%
        assert mock_sleep.call_args[0][0] == 1.1

@pytest.mark.asyncio
async def test_disk_safety_monitor_pause_resume(download_manager, mock_client):
    upload_allowed = asyncio.Event()
    monitor = DiskSafetyMonitor(
        download_manager=download_manager,
        client=mock_client,
        admin_channel_id=123,
        download_dir="/tmp",
        max_gb=10.0,
        resume_gb=5.0,
        batch_mode=True,
        upload_allowed=upload_allowed
    )

    download_manager.pause_new_downloads = AsyncMock()
    download_manager.pause_all = AsyncMock()
    download_manager.resume = AsyncMock()

    # Mock disk usage high
    with patch.object(monitor, "get_used_gb", return_value=12.0):
        await monitor.check_disk()
        assert monitor._is_paused == True
        assert upload_allowed.is_set() == True # Batch mode upload enabled
        download_manager.pause_all.assert_called_once()
        mock_client.send_message.assert_called_once()
        assert "Disk usage high" in mock_client.send_message.call_args[0][1]

    # Mock disk usage low again
    with patch.object(monitor, "get_used_gb", return_value=4.0):
        await monitor.check_disk()
        assert monitor._is_paused == False
        assert upload_allowed.is_set() == False # Batch mode upload disabled again
        download_manager.resume.assert_called_once()
        assert "Disk usage reduced" in mock_client.send_message.call_args[0][1]

@pytest.mark.asyncio
async def test_concurrency_workers(mock_client, state_persistence, tmp_path):
    queue = UploadQueue()
    worker1 = UploaderWorker(mock_client, queue, 123, 1000, state_persistence)
    worker2 = UploaderWorker(mock_client, queue, 123, 1000, state_persistence)

    # We won't start the full workers loop, we'll manually feed them tasks
    # to avoid mocking the entire while loop, we directly process tasks concurrently

    # Create files
    file1 = tmp_path / "1.txt"
    file1.write_bytes(b"A" * 10)
    file2 = tmp_path / "2.txt"
    file2.write_bytes(b"B" * 10)

    t1 = UploadTask(str(file1), 123, "1.txt", 10)
    t2 = UploadTask(str(file2), 123, "2.txt", 10)

    # Process them concurrently
    await asyncio.gather(
        worker1.process_task(t1),
        worker2.process_task(t2)
    )

    assert mock_client.send_document.call_count == 2
    assert not file1.exists()
    assert not file2.exists()
    assert state_persistence.mark_uploaded.call_count == 2

@pytest.mark.asyncio
async def test_restart_recovery(mock_client, state_persistence, download_manager, tmp_path):
    # This simulates BotApp's start logic
    state_persistence.load_state = AsyncMock(return_value={
        "queued_magnets": ["magnet1"],
        "uploading": [str(tmp_path / "completed.txt")],
        "uploaded": []
    })

    # Mock download_manager.get_status
    download_manager.get_status = AsyncMock(return_value={"numActive": "0", "numWaiting": "0"})
    download_manager.add_torrent = AsyncMock()

    # Simulate BotApp start recovery
    state = await state_persistence.load_state()
    status = await download_manager.get_status()
    num_active = int(status.get("numActive", 0)) if status else 0
    num_waiting = int(status.get("numWaiting", 0)) if status else 0

    if num_active == 0 and num_waiting == 0:
        for magnet in state.get("queued_magnets", []):
            url = magnet.get("url") if isinstance(magnet, dict) else magnet
            await download_manager.add_torrent(url)

    # Verify the magnet was requeued
    download_manager.add_torrent.assert_called_once_with("magnet1")

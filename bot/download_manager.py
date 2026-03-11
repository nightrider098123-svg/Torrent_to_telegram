# // Manages aria2 downloads via JSON-RPC, polling for completion and emitting events.
import aiohttp
import asyncio
import logging
import uuid
import os
from typing import Dict, Any, Callable, Coroutine, List
from dataclasses import dataclass
import json

logger = logging.getLogger(__name__)

@dataclass
class DownloadState:
    id: str
    name: str
    total_size: int
    downloaded: int
    progress: float
    status: str
    local_paths: List[str]

class DownloadManager:
    def __init__(self, download_dir: str, rpc_url: str = "http://localhost:6800/jsonrpc"):
        self.download_dir = download_dir
        self.rpc_url = rpc_url
        self.downloads: Dict[str, DownloadState] = {}
        self._on_file_complete_callbacks: list[Callable[[str, str], Coroutine]] = []
        self._on_download_complete_callbacks: list[Callable[[str], Coroutine]] = []
        self._is_paused = False
        self._poll_task = None
        self._rpc_session = None

    async def start(self):
        self._rpc_session = aiohttp.ClientSession()
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("Download manager started.")

    async def stop(self):
        if self._poll_task:
            self._poll_task.cancel()
        if self._rpc_session:
            await self._rpc_session.close()
        logger.info("Download manager stopped.")

    def register_on_file_complete(self, callback: Callable[[str, str], Coroutine]):
        self._on_file_complete_callbacks.append(callback)

    def register_on_download_complete(self, callback: Callable[[str], Coroutine]):
        self._on_download_complete_callbacks.append(callback)

    async def _call_rpc(self, method: str, params: list = None) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params or []
        }
        try:
            async with self._rpc_session.post(self.rpc_url, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    if "error" in data:
                        logger.error("Aria2 RPC Error", extra={"error": data["error"]})
                        return None
                    return data.get("result")
                else:
                    logger.error("Aria2 RPC Request Failed", extra={"error": response.status})
                    return None
        except Exception as e:
            logger.error("Aria2 RPC Request Exception", extra={"error": str(e)})
            return None

    async def add_torrent(self, magnet_or_torrent_path: str) -> str:
        if self._is_paused:
            logger.warning("Downloads are currently paused, but queueing anyway.")

        params = [magnet_or_torrent_path, {"dir": self.download_dir}]
        gid = await self._call_rpc("aria2.addUri", [[magnet_or_torrent_path], {"dir": self.download_dir}])
        if not gid:
            if magnet_or_torrent_path.endswith(".torrent"):
                import base64
                with open(magnet_or_torrent_path, "rb") as f:
                    torrent_data = base64.b64encode(f.read()).decode("utf-8")
                gid = await self._call_rpc("aria2.addTorrent", [torrent_data, [], {"dir": self.download_dir}])

        if gid:
            self.downloads[gid] = DownloadState(
                id=gid, name="", total_size=0, downloaded=0, progress=0, status="queued", local_paths=[]
            )
            logger.info(f"Added download task: {gid}", extra={"event_type": "download_started", "id": gid})
            return gid
        return None

    async def pause_new_downloads(self):
        self._is_paused = True
        logger.info("New downloads paused.")

    async def pause_all(self):
        self._is_paused = True
        await self._call_rpc("aria2.pauseAll")
        logger.info("All downloads paused.")

    async def resume(self):
        self._is_paused = False
        await self._call_rpc("aria2.unpauseAll")
        logger.info("Downloads resumed.")

    async def get_status(self) -> Dict[str, Any]:
        global_stat = await self._call_rpc("aria2.getGlobalStat")
        return global_stat

    async def _poll_loop(self):
        while True:
            try:
                await self._update_downloads()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Poll loop error", extra={"error": str(e)})
            await asyncio.sleep(5)

    async def _update_downloads(self):
        active = await self._call_rpc("aria2.tellActive") or []
        waiting = await self._call_rpc("aria2.tellWaiting", [0, 100]) or []
        stopped = await self._call_rpc("aria2.tellStopped", [0, 100]) or []

        all_tasks = active + waiting + stopped

        for task in all_tasks:
            gid = task.get("gid")
            if gid not in self.downloads:
                self.downloads[gid] = DownloadState(
                    id=gid, name="", total_size=0, downloaded=0, progress=0.0, status="", local_paths=[]
                )

            state = self.downloads[gid]

            completed_length = int(task.get("completedLength", 0))
            total_length = int(task.get("totalLength", 0))

            state.total_size = total_length
            state.downloaded = completed_length
            state.progress = (completed_length / total_length * 100) if total_length > 0 else 0.0

            prev_status = state.status
            state.status = task.get("status")

            # Log progress if downloading
            if state.status == "active":
                # To prevent log spam, only log if progress jumped by 10%
                if not hasattr(state, '_last_logged_progress'):
                    state._last_logged_progress = 0.0

                if state.progress - state._last_logged_progress >= 10.0:
                    state._last_logged_progress = state.progress
                    logger.info(f"Download Progress: {state.name or gid} {state.progress:.1f}%", extra={
                        "event_type": "download_progress",
                        "id": gid,
                        "filename": state.name or gid,
                        "size": state.total_size,
                        "progress": state.progress
                    })

            files = task.get("files", [])
            local_paths = []
            for f in files:
                path = f.get("path")
                # aria2 sets path to empty string if no file is allocated yet
                if path and path.startswith("[METADATA]") == False:
                    local_paths.append(path)
            state.local_paths = local_paths

            if local_paths:
                bt = task.get("bittorrent", {})
                info = bt.get("info", {})
                if info.get("name"):
                    state.name = info.get("name")
                else:
                    state.name = os.path.basename(local_paths[0])

            if state.status == "complete" and prev_status != "complete":
                logger.info(f"Download complete: {state.name}", extra={
                    "event_type": "download_completed",
                    "id": gid,
                    "filename": state.name,
                    "size": state.total_size
                })
                for local_path in state.local_paths:
                    if local_path and os.path.exists(local_path):
                        for cb in self._on_file_complete_callbacks:
                            await cb(gid, local_path)

                for cb in self._on_download_complete_callbacks:
                    await cb(gid)

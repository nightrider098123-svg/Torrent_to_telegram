import os
import time
import logging
import threading
import subprocess
import json
import uuid
from typing import Dict, Any, Callable, Optional
from queue_manager import Task

# Try to import libtorrent
try:
    import libtorrent as lt
    LIBTORRENT_AVAILABLE = True
except ImportError:
    lt = None
    LIBTORRENT_AVAILABLE = False
    logging.warning("libtorrent not available. Falling back to aria2c if possible.")

class DownloaderBackend:
    """Abstract base class for download backends."""
    def start_download(self, task: Task, download_dir: str, progress_callback: Callable[[Task], None]) -> Optional[str]:
        raise NotImplementedError

    def cancel(self, task_id: str) -> bool:
        raise NotImplementedError

    def get_status(self, task_id: str) -> Dict[str, Any]:
        raise NotImplementedError

class LibtorrentDownloader(DownloaderBackend):
    """
    Downloader backend using libtorrent bindings.
    This is the primary backend when available.
    """
    def __init__(self):
        self.session = lt.session()
        self.session.listen_on(6881, 6891)
        # Enable DHT and other standard extensions
        self.session.add_dht_router("router.bittorrent.com", 6881)
        self.session.add_dht_router("router.utorrent.com", 6881)
        self.session.add_dht_router("dht.transmissionbt.com", 6881)
        self.session.start_dht()

        self.handles: Dict[str, lt.torrent_handle] = {}
        self.threads: Dict[str, threading.Thread] = {}
        self.cancel_flags: Dict[str, bool] = {}

    def _download_thread(self, task: Task, handle: lt.torrent_handle, callback: Callable[[Task], None]):
        logging.info(f"Started libtorrent download for task {task.task_id}")

        # Wait for metadata
        while not handle.has_metadata():
            if self.cancel_flags.get(task.task_id, False):
                self.session.remove_torrent(handle)
                task.status = "cancelled"
                callback(task)
                return
            time.sleep(1)

        logging.info(f"Metadata acquired for {task.task_id}: {handle.status().name}")

        while not handle.is_seed():
            if self.cancel_flags.get(task.task_id, False):
                self.session.remove_torrent(handle)
                task.status = "cancelled"
                callback(task)
                return

            s = handle.status()
            task.progress = s.progress * 100
            task.downloaded_bytes = s.total_done
            task.total_bytes = s.total_wanted
            task.speed = s.download_rate
            task.peers = s.num_peers

            if s.download_rate > 0:
                task.eta = int((s.total_wanted - s.total_done) / s.download_rate)
            else:
                task.eta = 0

            task.status = "downloading"
            callback(task)
            time.sleep(5)

        logging.info(f"Download complete for task {task.task_id}")
        task.progress = 100.0
        task.status = "completed"
        callback(task)

        # Optionally wait before removing the handle to allow short-term seeding
        time.sleep(10)
        if task.task_id in self.handles:
            self.session.remove_torrent(handle)
            del self.handles[task.task_id]

    def start_download(self, task: Task, download_dir: str, progress_callback: Callable[[Task], None]) -> Optional[str]:
        params = {
            'save_path': download_dir,
            'storage_mode': lt.storage_mode_t.storage_mode_sparse,
        }

        try:
            if task.type == "magnet":
                handle = lt.add_magnet_uri(self.session, task.source, params)
            elif task.type == "torrent":
                info = lt.torrent_info(task.source)
                params['ti'] = info
                handle = self.session.add_torrent(params)
            else:
                raise ValueError("Unsupported task type")
        except Exception as e:
            logging.error(f"Failed to add torrent {task.task_id}: {e}")
            task.status = "failed"
            task.error = str(e)
            progress_callback(task)
            return None

        self.handles[task.task_id] = handle
        self.cancel_flags[task.task_id] = False

        t = threading.Thread(target=self._download_thread, args=(task, handle, progress_callback), daemon=True)
        self.threads[task.task_id] = t
        t.start()

        # Attempt to figure out single file path vs directory based on torrent handle
        # For magnets this might take time to get metadata
        return download_dir

    def cancel(self, task_id: str) -> bool:
        if task_id in self.handles:
            self.cancel_flags[task_id] = True
            return True
        return False

    def get_status(self, task_id: str) -> Dict[str, Any]:
        if task_id in self.handles:
            s = self.handles[task_id].status()
            return {
                "progress": s.progress * 100,
                "downloaded": s.total_done,
                "total": s.total_wanted,
                "speed": s.download_rate,
                "eta": int((s.total_wanted - s.total_done) / s.download_rate) if s.download_rate > 0 else 0,
                "peers": s.num_peers,
                "state": str(s.state)
            }
        return {}


class Aria2Downloader(DownloaderBackend):
    """
    Fallback downloader backend using aria2c over subprocess and JSON-RPC.
    Requires `aria2c` installed and in PATH.
    """
    def __init__(self):
        self.rpc_secret = str(uuid.uuid4())
        self.rpc_port = 6800
        self._start_aria2()
        self.active_tasks: Dict[str, str] = {} # task_id -> aria2_gid
        self.threads: Dict[str, threading.Thread] = {}
        self.cancel_flags: Dict[str, bool] = {}

    def _start_aria2(self):
        try:
            self.process = subprocess.Popen([
                "aria2c",
                "--enable-rpc",
                f"--rpc-listen-port={self.rpc_port}",
                f"--rpc-secret={self.rpc_secret}",
                "--rpc-listen-all=false",
                "--max-concurrent-downloads=5",
                "--continue=true",
                "--split=10",
                "--max-connection-per-server=10",
                "--min-split-size=10M",
                "--bt-save-metadata=true"
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logging.info(f"Started aria2c daemon with RPC port {self.rpc_port}")
            time.sleep(2) # Give aria2 time to bind port
        except FileNotFoundError:
            logging.error("aria2c not found in PATH. Please install it.")
            raise

    def __del__(self):
        if hasattr(self, 'process'):
            self.process.terminate()

    def _rpc_call(self, method: str, params: list = None) -> dict:
        import urllib.request
        import urllib.error

        if params is None:
            params = []

        payload = {
            "jsonrpc": "2.0",
            "id": "qbt",
            "method": method,
            "params": [f"token:{self.rpc_secret}"] + params
        }

        req = urllib.request.Request(
            f"http://127.0.0.1:{self.rpc_port}/jsonrpc",
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )

        try:
            with urllib.request.urlopen(req) as response:
                return json.loads(response.read().decode('utf-8'))
        except urllib.error.URLError as e:
            logging.error(f"Aria2 RPC Error: {e}")
            return {}

    def _download_thread(self, task: Task, gid: str, callback: Callable[[Task], None]):
        while True:
            if self.cancel_flags.get(task.task_id, False):
                self._rpc_call("aria2.remove", [gid])
                task.status = "cancelled"
                callback(task)
                break

            status = self._rpc_call("aria2.tellStatus", [gid])
            if not status or 'result' not in status:
                time.sleep(5)
                continue

            res = status['result']
            st = res.get('status')

            total = int(res.get('totalLength', 0))
            completed = int(res.get('completedLength', 0))
            speed = int(res.get('downloadSpeed', 0))

            task.total_bytes = total
            task.downloaded_bytes = completed
            task.speed = speed
            if speed > 0 and total > 0:
                task.eta = int((total - completed) / speed)
            else:
                task.eta = 0

            if total > 0:
                task.progress = (completed / total) * 100

            if st == 'active':
                task.status = "downloading"
                callback(task)
            elif st == 'complete':
                task.status = "completed"
                task.progress = 100.0
                callback(task)
                break
            elif st in ['error', 'removed']:
                task.status = "failed" if st == 'error' else "cancelled"
                task.error = res.get('errorMessage', '')
                callback(task)
                break

            time.sleep(5)

        if task.task_id in self.active_tasks:
            del self.active_tasks[task.task_id]

    def start_download(self, task: Task, download_dir: str, progress_callback: Callable[[Task], None]) -> Optional[str]:
        opts = {"dir": download_dir}
        gid = None
        if task.type == "magnet":
            resp = self._rpc_call("aria2.addUri", [[task.source], opts])
            gid = resp.get('result')
        elif task.type == "torrent":
            import base64
            with open(task.source, "rb") as f:
                torrent_data = base64.b64encode(f.read()).decode('utf-8')
            resp = self._rpc_call("aria2.addTorrent", [torrent_data, [], opts])
            gid = resp.get('result')

        if not gid:
            task.status = "failed"
            task.error = "Failed to add download to aria2c"
            progress_callback(task)
            return None

        self.active_tasks[task.task_id] = gid
        self.cancel_flags[task.task_id] = False
        t = threading.Thread(target=self._download_thread, args=(task, gid, progress_callback), daemon=True)
        self.threads[task.task_id] = t
        t.start()

        return download_dir

    def cancel(self, task_id: str) -> bool:
        if task_id in self.active_tasks:
            self.cancel_flags[task_id] = True
            return True
        return False

    def get_status(self, task_id: str) -> Dict[str, Any]:
        if task_id in self.active_tasks:
            gid = self.active_tasks[task_id]
            res = self._rpc_call("aria2.tellStatus", [gid]).get('result', {})
            return {
                "progress": float(res.get('completedLength', 0)) / max(1, float(res.get('totalLength', 1))) * 100,
                "downloaded": int(res.get('completedLength', 0)),
                "total": int(res.get('totalLength', 0)),
                "speed": int(res.get('downloadSpeed', 0))
            }
        return {}


def get_downloader() -> DownloaderBackend:
    """Returns the available downloader backend."""
    if LIBTORRENT_AVAILABLE:
        logging.info("ACTIVE BACKEND: libtorrent")
        return LibtorrentDownloader()
    else:
        logging.info("ACTIVE BACKEND: aria2c fallback")
        return Aria2Downloader()

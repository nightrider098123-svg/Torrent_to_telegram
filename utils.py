import os
import shutil
import math
import json
import logging
from configparser import ConfigParser
from typing import List, Dict, Any

# JSON formatter for structured logging
class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_record = {
            "time": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage()
        }
        if record.exc_info:
            log_record["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(log_record)

def setup_logging(log_file: str = "bot.log") -> None:
    """Configures structured JSON-lines logging to file and standard stream logging to console."""
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # Avoid adding handlers multiple times
    if logger.handlers:
        return

    # File handler (JSON lines)
    fh = logging.FileHandler(log_file)
    fh.setFormatter(JSONFormatter())
    logger.addHandler(fh)

    # Console handler (standard)
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(ch)

def load_config(config_path: str = "config.cfg") -> ConfigParser:
    """Loads configuration from the specified path, falling back to environment variables where necessary."""
    config = ConfigParser()
    config.read(config_path)

    env_mapping = {
        ("Bot", "admin_ids"): "BOT_ADMIN_IDS",
        ("Bot", "token"): "BOT_TOKEN",
        ("Bot", "max_auto_download_size_gb"): "BOT_MAX_AUTO_DOWNLOAD_SIZE_GB",
        ("Downloader", "download_dir"): "DOWNLOADER_DOWNLOAD_DIR",
        ("Downloader", "max_concurrent_downloads"): "DOWNLOADER_MAX_CONCURRENT_DOWNLOADS",
        ("Drive", "target_folder_id"): "DRIVE_TARGET_FOLDER_ID",
        ("Drive", "credentials_file"): "DRIVE_CREDENTIALS_FILE",
        ("Drive", "token_file"): "DRIVE_TOKEN_FILE",
        ("Telegram", "bot_token"): "TELEGRAM_BOT_TOKEN",
        ("Telegram", "owner_id"): "TELEGRAM_OWNER_ID",
        ("MTProto", "api_id"): "MTPROTO_API_ID",
        ("MTProto", "api_hash"): "MTPROTO_API_HASH",
        ("MTProto", "session_string"): "MTPROTO_SESSION_STRING",
        ("Upload", "use_userbot"): "UPLOAD_USE_USERBOT",
        ("Upload", "dump_channel"): "UPLOAD_DUMP_CHANNEL",
        ("Upload", "max_retries"): "UPLOAD_MAX_RETRIES",
        ("Upload", "max_concurrent_uploads"): "UPLOAD_MAX_CONCURRENT_UPLOADS",
        ("Upload", "telegram_file_limit_bytes"): "UPLOAD_TELEGRAM_FILE_LIMIT_BYTES",
        ("Storage", "use_gcs"): "STORAGE_USE_GCS",
        ("Storage", "gcp_project"): "STORAGE_GCP_PROJECT",
        ("Storage", "gcs_bucket"): "STORAGE_GCS_BUCKET",
        ("Storage", "gcp_service_account_json"): "STORAGE_GCP_SERVICE_ACCOUNT_JSON",
        ("Storage", "max_local_usage_gb"): "STORAGE_MAX_LOCAL_USAGE_GB",
        ("Storage", "download_dir"): "STORAGE_DOWNLOAD_DIR"
    }

    for (section, key), env_var in env_mapping.items():
        if env_var in os.environ:
            if not config.has_section(section):
                config.add_section(section)
            config.set(section, key, os.environ[env_var])

    return config

def rotate_token(config_path: str, section: str, key: str, new_token: str) -> None:
    """Helper script/function to rotate tokens in the configuration file."""
    config = ConfigParser()
    config.read(config_path)
    if section not in config:
        config.add_section(section)
    config.set(section, key, new_token)
    with open(config_path, 'w') as f:
        config.write(f)
    logging.info(f"Rotated {key} in {section}.")

def format_size(size_bytes: int) -> str:
    """Converts bytes to a human-readable format."""
    if size_bytes == 0:
        return "0B"
    size_name = ("B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"

def check_disk_space(path: str, required_bytes: int) -> bool:
    """Checks if there is enough disk space at the specified path."""
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)
    total, used, free = shutil.disk_usage(path)
    return free > required_bytes

def split_file_for_telegram(file_path: str, max_chunk_size: int = 1900 * 1024 * 1024) -> List[str]:
    """
    Splits a file into chunks smaller than max_chunk_size.
    Returns a list of chunk file paths.
    Telegram limit for bots is 50MB (via URL/File ID) or 20MB directly,
    but for local API server or premium it can be up to 2GB or 4GB.
    We assume the standard bot limit of 2GB for local bot API servers, or fallback limit.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    file_size = os.path.getsize(file_path)
    if file_size <= max_chunk_size:
        return [file_path]

    chunk_paths = []
    chunk_index = 0
    with open(file_path, 'rb') as f:
        while True:
            chunk_data = f.read(max_chunk_size)
            if not chunk_data:
                break

            # format: original_name.ext.001
            chunk_ext = f"{chunk_index + 1:03d}"
            chunk_path = f"{file_path}.{chunk_ext}"

            with open(chunk_path, 'wb') as chunk_file:
                chunk_file.write(chunk_data)

            chunk_paths.append(chunk_path)
            chunk_index += 1

    return chunk_paths

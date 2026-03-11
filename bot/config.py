import os
import logging
from dataclasses import dataclass

@dataclass
class Config:
    API_ID: int
    API_HASH: str
    USER_SESSION_STRING: str
    BOT_TOKEN: str
    DUMP_CHANNEL_ID: int
    ADMIN_CHANNEL_ID: int
    DOWNLOAD_DIR: str
    MAX_DISK_USED_GB: float
    RESUME_DISK_USED_GB: float
    UPLOAD_WORKERS: int
    SPLIT_SIZE_BYTES: int
    LOG_FILE: str
    BATCH_MODE: bool

def load_config() -> Config:
    api_id = int(os.environ["API_ID"])
    api_hash = os.environ["API_HASH"]
    user_session_string = os.environ.get("USER_SESSION_STRING", "")
    bot_token = os.environ.get("BOT_TOKEN", "")

    # Check what kind of connection we have and adjust split size defaults automatically
    split_size_str = os.environ.get("SPLIT_SIZE_BYTES", "")
    if split_size_str:
        split_size = int(split_size_str)
    else:
        # If user provides session string, we default to 4194304000 (3.9 GB)
        # If not, we default to 2147483648 (2.0 GB) for Bot API
        if user_session_string:
            split_size = 4194304000
        else:
            split_size = 2000000000 # Just under 2 GB to be safe

    return Config(
        API_ID=api_id,
        API_HASH=api_hash,
        USER_SESSION_STRING=user_session_string,
        BOT_TOKEN=bot_token,
        DUMP_CHANNEL_ID=int(os.environ["DUMP_CHANNEL_ID"]),
        ADMIN_CHANNEL_ID=int(os.environ["ADMIN_CHANNEL_ID"]),
        DOWNLOAD_DIR=os.environ.get("DOWNLOAD_DIR", "/content/downloads"),
        MAX_DISK_USED_GB=float(os.environ.get("MAX_DISK_USED_GB", "70.0")),
        RESUME_DISK_USED_GB=float(os.environ.get("RESUME_DISK_USED_GB", "40.0")),
        UPLOAD_WORKERS=int(os.environ.get("UPLOAD_WORKERS", "3")),
        SPLIT_SIZE_BYTES=split_size,
        LOG_FILE=os.environ.get("LOG_FILE", "/content/logs/bot.log"),
        BATCH_MODE=os.environ.get("BATCH_MODE", "false").lower() == "true",
    )

def setup_logging(log_file: str):
    import logging.handlers
    import json

    class JsonFormatter(logging.Formatter):
        def format(self, record):
            log_record = {
                "time": self.formatTime(record, self.datefmt),
                "level": record.levelname,
                "name": record.name,
                "message": record.getMessage()
            }
            if hasattr(record, "event_type"):
                log_record["event_type"] = record.event_type
            if hasattr(record, "id"):
                log_record["id"] = record.id
            if hasattr(record, "filename"):
                log_record["filename"] = record.filename
            if hasattr(record, "size"):
                log_record["size"] = record.size
            if hasattr(record, "progress"):
                log_record["progress"] = record.progress
            if hasattr(record, "parts"):
                log_record["parts"] = record.parts
            if hasattr(record, "duration"):
                log_record["duration"] = record.duration
            if record.exc_info:
                log_record["error"] = self.formatException(record.exc_info)
            elif hasattr(record, "error"):
                log_record["error"] = record.error
            return json.dumps(log_record)

    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5)
    handler.setFormatter(JsonFormatter())

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)
    root_logger.addHandler(console_handler)

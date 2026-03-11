import os
import json
import logging
import asyncio
from typing import Callable, Optional, List
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from queue_manager import Task
from utils import split_file_for_telegram
from userbot_uploader import UserbotUploader
from gcs_uploader import GCSUploader

SCOPES = ['https://www.googleapis.com/auth/drive.file']

class DriveUploader:
    def __init__(self, credentials_file: str, token_file: str):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.service = self._authenticate()

    def _authenticate(self):
        creds = None
        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as e:
                    logging.error(f"Failed to refresh Drive token: {e}")
                    creds = None
            if not creds:
                if not os.path.exists(self.credentials_file):
                    logging.warning(f"Google Drive credentials file {self.credentials_file} missing. Uploads will fail.")
                    return None
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(self.token_file, 'w') as token:
                token.write(creds.to_json())

        try:
            return build('drive', 'v3', credentials=creds, cache_discovery=False)
        except Exception as e:
            logging.error(f"Failed to build Drive service: {e}")
            return None

    def upload_file(self, file_path: str, folder_id: str, task: Task, progress_callback: Callable[[Task], None]) -> Optional[str]:
        if not self.service:
            task.status = "failed"
            task.error = "Google Drive service not authenticated."
            progress_callback(task)
            return None

        file_name = os.path.basename(file_path)
        file_metadata = {'name': file_name}
        if folder_id:
            file_metadata['parents'] = [folder_id]

        media = MediaFileUpload(file_path, resumable=True)
        request = self.service.files().create(body=file_metadata, media_body=media, fields='id')

        response = None
        task.status = "uploading"
        task.total_bytes = os.path.getsize(file_path)
        progress_callback(task)

        while response is None:
            if task.status == "cancelled":
                return None

            status, response = request.next_chunk()
            if status:
                task.progress = int(status.progress() * 100)
                task.downloaded_bytes = int(status.resumable_progress)
                progress_callback(task)

        task.status = "completed"
        task.progress = 100
        progress_callback(task)
        logging.info(f"File {file_name} uploaded successfully. Drive ID: {response.get('id')}")
        return response.get('id')

    def upload_directory(self, dir_path: str, parent_folder_id: str, task: Task, progress_callback: Callable[[Task], None]) -> bool:
        """Uploads a directory and its contents recursively to Google Drive."""
        if not self.service:
            task.status = "failed"
            task.error = "Google Drive service not authenticated."
            progress_callback(task)
            return False

        dir_name = os.path.basename(os.path.normpath(dir_path))
        folder_metadata = {
            'name': dir_name,
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [parent_folder_id] if parent_folder_id else []
        }

        try:
            folder = self.service.files().create(body=folder_metadata, fields='id').execute()
            new_folder_id = folder.get('id')
            logging.info(f"Created Drive folder {dir_name} with ID {new_folder_id}")

            for item in os.listdir(dir_path):
                if task.status == "cancelled":
                    return False

                item_path = os.path.join(dir_path, item)
                if os.path.isfile(item_path):
                    self.upload_file(item_path, new_folder_id, task, progress_callback)
                elif os.path.isdir(item_path):
                    self.upload_directory(item_path, new_folder_id, task, progress_callback)
            return True
        except Exception as e:
            logging.error(f"Failed to upload directory {dir_path}: {e}")
            task.status = "failed"
            task.error = str(e)
            progress_callback(task)
            return False

async def telegram_chunked_upload(file_path: str, context, chat_id: int, reply_to_message_id: Optional[int], max_chunk_size: int = 1900 * 1024 * 1024, userbot_uploader: Optional[UserbotUploader] = None) -> bool:
    """
    Splits a file and uploads it to Telegram chunk by chunk using sendDocument,
    or directly via MTProto if userbot_uploader is provided and file is within its limits.
    Returns True if upload is fully successful, False otherwise.
    """
    try:
        file_size = os.path.getsize(file_path)

        # If userbot is available and size is within MTProto limit (or we prefer to let userbot handle chunking if we implement it there)
        if userbot_uploader:
            actual_limit = userbot_uploader.telegram_file_limit_bytes
            if file_size <= actual_limit:
                logging.info(f"Using MTProto userbot to upload: {file_path}")
                return await userbot_uploader.upload_file(file_path)
            else:
                logging.info(f"File {file_path} exceeds userbot limit ({actual_limit}). Splitting.")
                max_chunk_size = actual_limit

        chunks = split_file_for_telegram(file_path, max_chunk_size)
        if len(chunks) > 1:
            msg = f"File is larger than Telegram limits. Split into {len(chunks)} chunks.\n"
            msg += "To combine them, run: `cat filename.ext.* > filename.ext`"
            if reply_to_message_id:
                await context.bot.send_message(chat_id=chat_id, reply_to_message_id=reply_to_message_id, text=msg, parse_mode='Markdown')
            else:
                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')

        for i, chunk in enumerate(chunks):
            if userbot_uploader:
                logging.info(f"Uploading chunk {i+1}/{len(chunks)} via MTProto")
                success = await userbot_uploader.upload_file(chunk)
                if not success:
                    raise Exception(f"MTProto upload failed for chunk {i+1}")
            else:
                with open(chunk, 'rb') as chunk_file:
                    kwargs = {
                        "chat_id": chat_id,
                        "document": chunk_file,
                        "caption": f"Part {i+1} of {len(chunks)}"
                    }
                    if reply_to_message_id:
                        kwargs["reply_to_message_id"] = reply_to_message_id

                    await context.bot.send_document(**kwargs)

            # Small delay to prevent rate limiting
            await asyncio.sleep(1)

            # Clean up chunks after upload (but not the original if it wasn't split)
            if chunk != file_path:
                os.remove(chunk)

        return True

    except Exception as e:
        logging.error(f"Telegram upload failed: {e}")
        error_msg = f"Telegram upload failed: {e}"
        if reply_to_message_id:
            await context.bot.send_message(chat_id=chat_id, reply_to_message_id=reply_to_message_id, text=error_msg)
        else:
            await context.bot.send_message(chat_id=chat_id, text=error_msg)
        return False

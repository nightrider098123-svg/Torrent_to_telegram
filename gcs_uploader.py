import os
import logging
from google.cloud import storage
from google.oauth2 import service_account
from google.api_core.exceptions import GoogleAPIError

class GCSUploader:
    def __init__(self, gcp_project: str, gcs_bucket: str, service_account_json: str):
        self.gcp_project = gcp_project
        self.gcs_bucket = gcs_bucket
        self.service_account_json = service_account_json
        self.client = None
        self._authenticate()

    def _authenticate(self):
        try:
            if not os.path.exists(self.service_account_json):
                logging.error(f"GCS Service account JSON not found at: {self.service_account_json}")
                return

            credentials = service_account.Credentials.from_service_account_file(self.service_account_json)
            self.client = storage.Client(project=self.gcp_project, credentials=credentials)
            logging.info("Successfully authenticated with Google Cloud Storage.")
        except Exception as e:
            logging.error(f"Failed to authenticate with GCS: {e}")

    def upload_file(self, file_path: str, destination_blob_name: str) -> str:
        """Uploads a file to the bucket with resumable uploads."""
        if not self.client:
            logging.error("GCS Client not authenticated. Cannot upload.")
            return ""

        try:
            bucket = self.client.bucket(self.gcs_bucket)
            blob = bucket.blob(destination_blob_name)

            # Use chunked (resumable) upload
            blob.chunk_size = 5 * 1024 * 1024 # 5 MB chunks

            logging.info(f"Uploading {file_path} to GCS as {destination_blob_name}")
            blob.upload_from_filename(file_path)

            gcs_url = f"gs://{self.gcs_bucket}/{destination_blob_name}"
            logging.info(f"Successfully uploaded to GCS: {gcs_url}")
            return gcs_url
        except GoogleAPIError as e:
            logging.error(f"GCS API Error during upload: {e}")
            return ""
        except Exception as e:
            logging.error(f"Unexpected error during GCS upload: {e}")
            return ""

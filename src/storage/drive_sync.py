import logging
import os
import subprocess
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class GoogleDriveSync:
    """
    Cloud sync provider for Google Drive.
    Wraps the gog CLI or standard storage APIs to upload artifacts.
    """

    def __init__(self, parent_folder_id: Optional[str] = None):
        self.parent_folder_id = parent_folder_id or os.getenv("DRIVE_PARENT_FOLDER_ID")
        self.gog_bin = os.getenv("GOG_BIN", "/home/linuxbrew/.linuxbrew/bin/gog")
        self.keyring_password = os.getenv("GOG_KEYRING_PASSWORD", "")

    def upload_file(self, local_path: str, parent_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Uploads a local file to Google Drive."""
        target_parent = parent_id or self.parent_folder_id
        if not target_parent:
            logger.warning("No Google Drive parent folder ID configured. Skipping cloud upload.")
            return None

        if not os.path.exists(local_path):
            logger.error(f"Cannot upload missing file: {local_path}")
            return None

        env = os.environ.copy()
        if self.keyring_password:
            env["GOG_KEYRING_PASSWORD"] = self.keyring_password

        cmd = [self.gog_bin, "drive", "upload", local_path, f"--parent={target_parent}", "-j"]
        try:
            res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
            if res.returncode == 0:
                import json
                data = json.loads(res.stdout)
                file_info = data.get("file", {})
                logger.info(f"Successfully uploaded {local_path} to Drive ID: {file_info.get('id')}")
                return file_info
            else:
                logger.error(f"gog drive upload failed: {res.stderr}")
                return None
        except Exception as e:
            logger.error(f"Drive upload exception: {e}")
            return None

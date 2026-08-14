"""
資料庫定期備份到Google Drive
沿用其他bot共用的服務帳號金鑰，不用重新設定OAuth
"""
import os
import shutil
from datetime import datetime, timezone, timedelta

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2 import service_account

import config

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER_ID = "1TeZJP5V1y1lbkrqHqN7--CR83OB315IY"  # 「秘書Bot資料庫備份」資料夾
SERVICE_ACCOUNT_PATH = "/root/luna_bot/service_account.json"
KEEP_BACKUPS = 8  # 保留最近幾份備份（每週一份，約兩個月）

TW_TZ = timezone(timedelta(hours=config.TAIWAN_TZ_OFFSET_HOURS))


def _get_drive_service():
    if not os.path.exists(SERVICE_ACCOUNT_PATH):
        raise FileNotFoundError(f"找不到服務帳號金鑰：{SERVICE_ACCOUNT_PATH}")
    creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_PATH, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def backup_database() -> str:
    """把目前的secretary.db複製一份並上傳到Drive，回傳上傳後的檔案id"""
    if not os.path.exists(config.DB_PATH):
        raise FileNotFoundError(f"找不到資料庫檔案：{config.DB_PATH}")

    service = _get_drive_service()

    timestamp = datetime.now(TW_TZ).strftime("%Y%m%d_%H%M%S")
    filename = f"secretary_backup_{timestamp}.db"
    tmp_path = f"/tmp/{filename}"

    # 先複製一份再上傳，避免備份當下資料庫正被寫入造成鎖定問題
    shutil.copy2(config.DB_PATH, tmp_path)

    try:
        file_metadata = {"name": filename, "parents": [FOLDER_ID]}
        media = MediaFileUpload(tmp_path, mimetype="application/x-sqlite3")
        uploaded = service.files().create(body=file_metadata, media_body=media, fields="id").execute()
        return uploaded.get("id")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def cleanup_old_backups(keep: int = KEEP_BACKUPS):
    """只保留最近keep份備份，避免資料夾檔案無限增加"""
    service = _get_drive_service()
    results = service.files().list(
        q=f"'{FOLDER_ID}' in parents and trashed=false",
        orderBy="createdTime desc",
        fields="files(id, name, createdTime)",
        pageSize=100,
    ).execute()
    files = results.get("files", [])
    for f in files[keep:]:
        service.files().delete(fileId=f["id"]).execute()

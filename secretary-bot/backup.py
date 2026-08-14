"""
資料庫定期備份到Google Drive
沿用其他bot共用的服務帳號金鑰，不用重新設定OAuth

注意：Google服務帳號本身沒有儲存額度，無法用它的身分「建立新檔案」。
解法是用真人帳號預先建好一個佔位檔案（BACKUP_FILE_ID），
備份時服務帳號只「更新」這個既有檔案的內容，不會碰到額度問題。
Google Drive本身會自動保留檔案的修訂歷史（revision），
如果需要復原到更早的版本，到該檔案點右鍵「管理版本」就能看到。
"""
import os
import shutil
from datetime import datetime, timezone, timedelta

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2 import service_account

import config

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
BACKUP_FILE_ID = "1OYA4_pcvlPrEAjpyrYAol4H1RV5to59I"  # secretary_backup_latest.db（真人帳號預先建立）
SERVICE_ACCOUNT_PATH = "/root/luna_bot/service_account.json"

TW_TZ = timezone(timedelta(hours=config.TAIWAN_TZ_OFFSET_HOURS))


def _get_drive_service():
    if not os.path.exists(SERVICE_ACCOUNT_PATH):
        raise FileNotFoundError(f"找不到服務帳號金鑰：{SERVICE_ACCOUNT_PATH}")
    creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_PATH, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def backup_database() -> str:
    """把目前的secretary.db複製一份，更新到既有的Drive備份檔案裡，回傳檔案id"""
    if not os.path.exists(config.DB_PATH):
        raise FileNotFoundError(f"找不到資料庫檔案：{config.DB_PATH}")

    service = _get_drive_service()

    timestamp = datetime.now(TW_TZ).strftime("%Y%m%d_%H%M%S")
    tmp_path = f"/tmp/secretary_backup_{timestamp}.db"

    # 先複製一份再上傳，避免備份當下資料庫正被寫入造成鎖定問題
    shutil.copy2(config.DB_PATH, tmp_path)

    try:
        media = MediaFileUpload(tmp_path, mimetype="application/x-sqlite3")
        updated = service.files().update(fileId=BACKUP_FILE_ID, media_body=media, fields="id, modifiedTime").execute()
        return updated.get("id")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

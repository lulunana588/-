"""
資料庫定期備份到Google Drive
沿用其他bot共用的服務帳號金鑰，不用重新設定OAuth

注意：Google服務帳號本身沒有儲存額度，無法用它的身分「建立新檔案」。
解法是用真人帳號預先建好一個佔位檔案（BACKUP_FILE_ID），
備份時服務帳號只「更新」這個既有檔案的內容，不會碰到額度問題。
Google Drive本身會自動保留檔案的修訂歷史（revision），
如果需要復原到更早的版本，到該檔案點右鍵「管理版本」就能看到。

備份完成後會做兩層驗證，確保「有備份」等於「真的能還原」：
  1. 上傳前：確認本地複本是合法、完整的SQLite資料庫（PRAGMA integrity_check）
  2. 上傳後：把Drive上剛更新的檔案下載回來，重新驗證一次完整性，
     並核對資料筆數跟本地資料庫一致，確保上傳過程沒有損毀
"""
import os
import shutil
import sqlite3
from datetime import datetime, timezone, timedelta

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2 import service_account

import config
import db

SCOPES = ["https://www.googleapis.com/auth/drive"]
BACKUP_FILE_ID = "1OYA4_pcvlPrEAjpyrYAol4H1RV5to59I"  # secretary_backup_latest.db（真人帳號預先建立）
SERVICE_ACCOUNT_PATH = "/root/luna_bot/service_account.json"
BACKUP_TABLES = ("tasks", "leaves", "templates")

TW_TZ = timezone(timedelta(hours=config.TAIWAN_TZ_OFFSET_HOURS))


def _get_drive_service():
    if not os.path.exists(SERVICE_ACCOUNT_PATH):
        raise FileNotFoundError(f"找不到服務帳號金鑰：{SERVICE_ACCOUNT_PATH}")
    creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_PATH, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def _verify_sqlite_integrity(path: str) -> bool:
    """檢查檔案是否為合法且完整、沒有損毀的SQLite資料庫"""
    try:
        conn = sqlite3.connect(path)
        row = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        return bool(row) and row[0] == "ok"
    except Exception:
        return False


def _count_rows(path: str) -> int:
    """統計三張核心資料表的總筆數，用來做上傳前後的資料一致性核對"""
    total = 0
    try:
        conn = sqlite3.connect(path)
        for table in BACKUP_TABLES:
            try:
                total += conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                pass  # 表不存在就跳過，不影響其他表的核對
        conn.close()
    except Exception:
        pass
    return total


def backup_database() -> str:
    """
    把目前的secretary.db複製一份、驗證完整性後上傳到既有的Drive備份檔案，
    上傳完再下載回來做二次驗證，確保雲端上的版本真的可用。
    回傳Drive檔案id；任何一關驗證沒過都會拋出例外，不會回報「成功」。
    """
    if not os.path.exists(config.DB_PATH):
        raise FileNotFoundError(f"找不到資料庫檔案：{config.DB_PATH}")

    service = _get_drive_service()

    timestamp = datetime.now(TW_TZ).strftime("%Y%m%d_%H%M%S")
    tmp_path = f"/tmp/secretary_backup_{timestamp}.db"
    verify_path = f"/tmp/secretary_verify_{timestamp}.db"

    # 先複製一份再上傳，避免備份當下資料庫正被寫入造成鎖定問題
    shutil.copy2(config.DB_PATH, tmp_path)

    try:
        if not _verify_sqlite_integrity(tmp_path):
            raise RuntimeError("備份前的本地複本資料庫已損毀，中止上傳")

        local_count = _count_rows(tmp_path)

        media = MediaFileUpload(tmp_path, mimetype="application/x-sqlite3")
        updated = service.files().update(
            fileId=BACKUP_FILE_ID, media_body=media, fields="id, modifiedTime, size"
        ).execute()

        # 上傳後把Drive上的檔案下載回來，驗證傳輸過程沒有損毀
        content = service.files().get_media(fileId=BACKUP_FILE_ID).execute()
        with open(verify_path, "wb") as f:
            f.write(content)

        if not _verify_sqlite_integrity(verify_path):
            raise RuntimeError("備份上傳後驗證失敗：Drive上的檔案已損毀，請重新備份")

        remote_count = _count_rows(verify_path)
        if remote_count != local_count:
            raise RuntimeError(
                f"備份資料筆數不一致（本地{local_count}筆、雲端{remote_count}筆），備份可能不完整"
            )

        db.set_meta("last_backup_at", datetime.now(TW_TZ).isoformat())
        return updated.get("id")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        if os.path.exists(verify_path):
            os.remove(verify_path)

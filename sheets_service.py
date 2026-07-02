# sheets_service.py
# 封裝所有跟 Google Sheets 直接讀寫的邏輯（用服務帳戶 gspread，不用 GAS）

import datetime
import gspread
from google.oauth2.service_account import Credentials

import config

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

_client = None


def get_client():
    """建立/重用 gspread client（服務帳戶授權）"""
    global _client
    if _client is None:
        creds = Credentials.from_service_account_file(
            config.GOOGLE_SERVICE_ACCOUNT_JSON, scopes=SCOPES
        )
        _client = gspread.authorize(creds)
    return _client


def _open_worksheet(sheet_id: str, gid: int):
    sh = get_client().open_by_key(sheet_id)
    return sh.get_worksheet_by_id(gid)


def today_str() -> str:
    return datetime.date.today().strftime("%Y/%m/%d")


# =========================================================
# 桶裝水庫存表
# =========================================================

def get_water_worksheet():
    return _open_worksheet(config.WATER_SHEET_ID, config.WATER_GID)


def list_water_locations():
    """
    回傳目前表上所有地點資料。
    自動尋找標題列（第一格內容為「地點」的那一列），不寫死行號，
    這樣未來她自己在表上增減地點列，機器人不用改程式碼。
    回傳: [{row, location, supplier, last_update, stock, note, status}, ...]
    """
    ws = get_water_worksheet()
    all_values = ws.get_all_values()

    header_row_idx = None
    for i, row in enumerate(all_values):
        if row and row[0].strip() == "地點":
            header_row_idx = i
            break
    if header_row_idx is None:
        raise RuntimeError("找不到桶裝水表的標題列（地點/供應商/...），請確認表格式未被改動")

    locations = []
    for i in range(header_row_idx + 1, len(all_values)):
        row = all_values[i]
        if not row or not row[0].strip():
            continue  # 空白列或已到備註列
        if row[0].strip().startswith("⚠️") or "剩餘" in row[0]:
            break  # 碰到底部備註列就停止
        location = row[0].strip()
        supplier = row[1].strip() if len(row) > 1 else ""
        last_update = row[2].strip() if len(row) > 2 else ""
        stock_raw = row[3].strip() if len(row) > 3 else "0"
        note = row[4].strip() if len(row) > 4 else ""
        status = row[5].strip() if len(row) > 5 else ""
        try:
            stock = int(stock_raw)
        except ValueError:
            stock = 0
        locations.append(
            {
                "row": i + 1,  # gspread 是 1-indexed
                "location": location,
                "supplier": supplier,
                "last_update": last_update,
                "stock": stock,
                "note": note,
                "status": status,
            }
        )
    return locations


def compute_water_status(stock: int) -> str:
    if stock <= config.WATER_LOW_STOCK_THRESHOLD:
        return "⚠️ 需補貨"
    return "✅ 正常"


def update_water_stock(row: int, delta: int):
    """
    對指定列的庫存做加減，並自動更新「最後更新日期」與「狀態」欄。
    回傳更新後的完整資料 dict。
    """
    ws = get_water_worksheet()
    current = ws.row_values(row)
    stock_raw = current[3] if len(current) > 3 else "0"
    try:
        old_stock = int(stock_raw)
    except ValueError:
        old_stock = 0

    new_stock = max(0, old_stock + delta)
    new_date = today_str()
    new_status = compute_water_status(new_stock)

    # C欄=最後更新日期, D欄=目前庫存, F欄=狀態
    ws.update(f"C{row}:D{row}", [[new_date, new_stock]])
    ws.update(f"F{row}", [[new_status]])

    return {
        "location": current[0] if current else "",
        "old_stock": old_stock,
        "new_stock": new_stock,
        "date": new_date,
        "status": new_status,
    }


# =========================================================
# 款項追蹤表
# =========================================================

def get_payment_worksheet():
    return _open_worksheet(config.PAYMENT_SHEET_ID, config.PAYMENT_GID)


def _payment_header_row_idx(all_values):
    for i, row in enumerate(all_values):
        if row and row[0].strip() == "編號":
            return i
    raise RuntimeError("找不到款項追蹤表的標題列（編號/送件日期/...），請確認表格式未被改動")


def get_next_payment_id(all_values=None, header_idx=None) -> int:
    ws = get_payment_worksheet()
    all_values = all_values or ws.get_all_values()
    header_idx = header_idx if header_idx is not None else _payment_header_row_idx(all_values)
    max_id = 0
    for row in all_values[header_idx + 1 :]:
        if not row or not row[0].strip():
            continue
        try:
            n = int(row[0].strip())
            max_id = max(max_id, n)
        except ValueError:
            continue
    return max_id + 1


def add_payment_record(name: str, amount: str, submit_date: str, progress: str):
    """
    新增一筆款項紀錄。編號自動遞增，付款狀態預設「待付」，實付日期/備註留空。
    """
    ws = get_payment_worksheet()
    all_values = ws.get_all_values()
    header_idx = _payment_header_row_idx(all_values)
    new_id = get_next_payment_id(all_values, header_idx)

    # 找到標題列之後、第一個「完全空白」的列，寫在那一列（避免 append_row 因格式化空白列跑到很後面）
    target_row = None
    for i in range(header_idx + 1, len(all_values)):
        row = all_values[i]
        if not row or not any(cell.strip() for cell in row):
            target_row = i + 1  # 1-indexed
            break
    if target_row is None:
        target_row = len(all_values) + 1

    ws.update(
        f"A{target_row}:H{target_row}",
        [[new_id, submit_date, name, amount, progress, "待付", "", ""]],
    )
    return {
        "id": new_id,
        "row": target_row,
        "name": name,
        "amount": amount,
        "submit_date": submit_date,
        "progress": progress,
    }


def find_payment_records(query: str, limit: int = 8):
    """依編號完全比對優先，否則用款項名稱模糊比對"""
    ws = get_payment_worksheet()
    all_values = ws.get_all_values()
    header_idx = _payment_header_row_idx(all_values)

    results = []
    query = query.strip()

    for i in range(header_idx + 1, len(all_values)):
        row = all_values[i]
        if not row or not row[0].strip():
            continue
        row_id = row[0].strip()
        name = row[2].strip() if len(row) > 2 else ""
        if query == row_id:
            results.insert(0, {"row": i + 1, "values": row})  # 精準比對排最前面
        elif query and query in name:
            results.append({"row": i + 1, "values": row})
        if len(results) >= limit:
            break
    return results[:limit]


def mark_payment_paid(row: int, paid_date: str = None, note: str = None):
    """把付款狀態改為已付，並帶入實付日期；若有給 note 則附加到備註欄"""
    ws = get_payment_worksheet()
    paid_date = paid_date or today_str()
    current = ws.row_values(row)
    old_note = current[7] if len(current) > 7 else ""

    ws.update(f"F{row}:G{row}", [["已付", paid_date]])
    if note:
        combined_note = f"{old_note}；{note}" if old_note else note
        ws.update(f"H{row}", [[combined_note]])

    return {
        "id": current[0] if current else "",
        "name": current[2] if len(current) > 2 else "",
        "amount": current[3] if len(current) > 3 else "",
        "paid_date": paid_date,
    }

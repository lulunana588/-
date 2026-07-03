# -*- coding: utf-8 -*-
"""
逐筆資產清冊讀寫工具
每個辦公室的試算表底下有「辦公室資產」「資訊類資產」兩個分頁,
欄位結構一致:編號/名稱/規格/序號/採購單位/使用狀況/所在公司/所在區域/
使用部門/員編/保管人/入庫日期/領取日期/規格/備註
"""
import datetime
import gspread
from google.oauth2.service_account import Credentials
from config import (
    GOOGLE_SERVICE_ACCOUNT_FILE,
    OFFICES,
    DETAIL_SHEETS,
    COLUMNS,
    HEADER_ROW,
    LOCAL_LOG_SHEET,
    TRANSFER_LOG_SHEET,
)

TAIPEI_TZ = datetime.timezone(datetime.timedelta(hours=8))


def today_str():
    return datetime.datetime.now(TAIPEI_TZ).strftime("%Y/%m/%d")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_client = None
_ws_cache = {}  # (office, sheet_name) -> gspread Worksheet


def get_client():
    global _client
    if _client is None:
        creds = Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        _client = gspread.authorize(creds)
    return _client


def get_worksheet(office: str, sheet_name: str):
    key = (office, sheet_name)
    if key not in _ws_cache:
        sh = get_client().open_by_key(OFFICES[office]["spreadsheet_id"])
        _ws_cache[key] = sh.worksheet(sheet_name)
    return _ws_cache[key]


def _col_to_index(col_letter: str) -> int:
    return ord(col_letter.upper()) - ord("A") + 1


def _row_to_record(row_values):
    record = {}
    for key, col in COLUMNS.items():
        idx = _col_to_index(col) - 1
        record[key] = row_values[idx].strip() if idx < len(row_values) and row_values[idx] else ""
    return record


def find_asset(office: str, asset_id: str):
    """
    依編號在該辦公室的所有 DETAIL_SHEETS 中搜尋(去除頭尾空白、忽略大小寫)。
    回傳 (sheet_name, row_number, record_dict) 或 None
    """
    target = asset_id.strip().upper()
    id_idx = _col_to_index(COLUMNS["id"]) - 1
    for sheet_name in DETAIL_SHEETS:
        ws = get_worksheet(office, sheet_name)
        values = ws.get_all_values()
        for i, row in enumerate(values):
            row_num = i + 1
            if row_num <= HEADER_ROW:
                continue
            if id_idx < len(row) and row[id_idx].strip().upper() == target:
                return sheet_name, row_num, _row_to_record(row)
    return None


def find_asset_any_office(asset_id: str):
    """
    不指定辦公室,自動在所有 OFFICES 底下搜尋這個編號。
    回傳一個 list,每個元素是 (office, sheet_name, row_number, record_dict)。
    正常情況下應該只會有 0 或 1 筆;如果同一編號在兩間辦公室都存在,
    會回傳多筆,呼叫端應提示使用者手動指定辦公室。
    """
    matches = []
    for office in OFFICES:
        found = find_asset(office, asset_id)
        if found:
            sheet_name, row, record = found
            matches.append((office, sheet_name, row, record))
    return matches


def update_field(office: str, sheet_name: str, row: int, field_key: str, value: str):
    """更新單一欄位(所在區域/使用部門/員編/保管人/使用狀況等)"""
    ws = get_worksheet(office, sheet_name)
    col = COLUMNS[field_key]
    ws.update(f"{col}{row}", [[value]])


def update_fields(office: str, sheet_name: str, row: int, fields: dict):
    """一次更新多個欄位(例如 {'status': '使用中', 'keeper': '小美'}),減少 API 呼叫次數"""
    if not fields:
        return
    ws = get_worksheet(office, sheet_name)
    data = []
    for field_key, value in fields.items():
        col = COLUMNS[field_key]
        data.append({"range": f"{col}{row}", "values": [[value]]})
    ws.batch_update(data)


def append_note(office: str, sheet_name: str, row: int, text: str):
    """
    在備註欄的「儲存格註解」新增一行「日期 說明」,保留歷史紀錄,
    不覆蓋原本備註欄顯示的文字。
    """
    ws = get_worksheet(office, sheet_name)
    col = COLUMNS["note"]
    cell = f"{col}{row}"
    today = datetime.datetime.now().strftime("%Y/%m/%d")
    new_line = f"{today} {text}"
    try:
        existing = ws.get_note(cell) or ""
    except Exception:
        existing = ""
    combined = f"{existing}\n{new_line}" if existing else new_line
    ws.update_note(cell, combined)
    return combined


def get_note(office: str, sheet_name: str, row: int):
    ws = get_worksheet(office, sheet_name)
    col = COLUMNS["note"]
    try:
        return ws.get_note(f"{col}{row}") or ""
    except Exception:
        return ""


def append_local_log(office: str, task: str, person: str, description: str, asset_id: str, name: str, spec: str):
    """寫一筆到「本点管理」分頁:任務/日期/花名/說明/編號/名稱/規格"""
    ws = get_worksheet(office, LOCAL_LOG_SHEET)
    ws.append_row(
        [task, today_str(), person, description, asset_id, name, spec],
        value_input_option="USER_ENTERED",
    )


def append_transfer_log(office: str, task: str, department: str, description: str, asset_id: str, name: str, spec: str):
    """寫一筆到「跨點調撥」分頁:任務/日期/部門/說明/編號/名稱/規格"""
    ws = get_worksheet(office, TRANSFER_LOG_SHEET)
    ws.append_row(
        [task, today_str(), department, description, asset_id, name, spec],
        value_input_option="USER_ENTERED",
    )


def create_asset(office: str, category: str, asset_id: str, name: str, spec: str, location: str):
    """
    在指定辦公室的 category 分頁(辦公室資產/資訊類資產)新增一整列全新資產。
    使用狀況預設「庫存」,所在公司=office,所在區域=傳入值,其餘欄位留空。
    回傳新資產所在的列號。
    """
    ws = get_worksheet(office, category)
    values = ws.get_all_values()
    target_row = len(values) + 1

    field_values = {
        "id": asset_id,
        "name": name,
        "spec": spec,
        "status": "庫存",
        "company": office,
        "location": location,
    }
    max_col_index = max(_col_to_index(c) for c in COLUMNS.values())
    full_row = [""] * max_col_index
    for key, val in field_values.items():
        idx = _col_to_index(COLUMNS[key]) - 1
        full_row[idx] = val

    ws.update(f"A{target_row}", [full_row])
    return target_row

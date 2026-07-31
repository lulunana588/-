# -*- coding: utf-8 -*-
"""
門號(SIM卡)試算表讀寫工具
商務中心 / 共享服務中心 各自一份試算表,欄位結構略有不同(共享服務中心多一個「門號類型」欄),
統一用 SIM_OFFICES[office]["columns"] 這個 dict 來對應欄位代號 -> 欄位字母。
"""
import re
import datetime
import gspread
from google.oauth2.service_account import Credentials
from config import GOOGLE_SERVICE_ACCOUNT_FILE, SIM_OFFICES

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TAIPEI_TZ = datetime.timezone(datetime.timedelta(hours=8))

_client = None
_ws_cache = {}


def today_str():
    return datetime.datetime.now(TAIPEI_TZ).strftime("%Y/%m/%d")


def get_client():
    global _client
    if _client is None:
        creds = Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        _client = gspread.authorize(creds)
    return _client


def get_sim_worksheet(office: str):
    key = office
    if key not in _ws_cache:
        info = SIM_OFFICES[office]
        sh = get_client().open_by_key(info["spreadsheet_id"])
        _ws_cache[key] = sh.worksheet(info["sheet_name"])
    return _ws_cache[key]


def _col_to_index(col_letter: str) -> int:
    return ord(col_letter.upper()) - ord("A") + 1


def _normalize_number(number: str) -> str:
    """只留數字,用來比對門號是否相同,不管有沒有打「-」"""
    return re.sub(r"\D", "", number or "")


def _row_to_record(office: str, row_values):
    columns = SIM_OFFICES[office]["columns"]
    record = {}
    for key, col in columns.items():
        idx = _col_to_index(col) - 1
        record[key] = row_values[idx].strip() if idx < len(row_values) and row_values[idx] else ""
    return record


def find_sim(office: str, phone_number: str):
    """
    依門號在該辦公室試算表中搜尋(比對時忽略「-」符號)。
    回傳 (row_number, record_dict) 或 None
    """
    target = _normalize_number(phone_number)
    if not target:
        return None
    columns = SIM_OFFICES[office]["columns"]
    number_idx = _col_to_index(columns["number"]) - 1
    ws = get_sim_worksheet(office)
    values = ws.get_all_values()
    for i, row in enumerate(values):
        row_num = i + 1
        if row_num <= 1:
            continue
        if number_idx < len(row) and _normalize_number(row[number_idx]) == target:
            return row_num, _row_to_record(office, row)
    return None


def find_sim_any_office(phone_number: str):
    """
    不指定辦公室,自動在所有 SIM_OFFICES 底下搜尋這個門號。
    回傳 list,每個元素是 (office, row_number, record_dict)。
    """
    matches = []
    for office in SIM_OFFICES:
        found = find_sim(office, phone_number)
        if found:
            row, record = found
            matches.append((office, row, record))
    return matches


def update_sim_fields(office: str, row: int, fields: dict):
    """一次更新多個欄位(例如 {'name': '小美', 'type': '公務機'})"""
    if not fields:
        return
    columns = SIM_OFFICES[office]["columns"]
    ws = get_sim_worksheet(office)
    data = []
    for field_key, value in fields.items():
        col = columns[field_key]
        data.append({"range": f"{col}{row}", "values": [[value]]})
    ws.batch_update(data)


def append_sim_note(office: str, row: int, text: str):
    """
    在附註欄「累加」一行「日期 說明」,保留歷史紀錄(直接寫在儲存格文字裡,
    因為這份表原本的附註就是用顯示文字,不是用註解)。
    """
    columns = SIM_OFFICES[office]["columns"]
    col = columns["note"]
    ws = get_sim_worksheet(office)
    cell = f"{col}{row}"
    try:
        existing = (ws.acell(cell).value or "").rstrip()
    except Exception:
        existing = ""
    new_line = f"{today_str()} {text}"
    combined = f"{existing}\n{new_line}" if existing else new_line
    ws.update(cell, [[combined]], value_input_option="USER_ENTERED")
    return combined

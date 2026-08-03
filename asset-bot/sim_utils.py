# -*- coding: utf-8 -*-
"""
門號(SIM卡)試算表讀寫工具

改用 Google Sheets API 的輕量 values.* 端點直接讀寫(不透過 gspread 的
open_by_key,因為那個方式會先抓整份試算表的完整中繼資料,商務中心門號資訊
這份表用了進階的下拉標籤/chips功能,完整讀取中繼資料時 Google 那邊常常
回傳 500 錯誤。改成只針對需要的欄位範圍直接讀寫,就不會碰到那個問題)。
"""
import re
import datetime
import urllib.parse
import requests
import google.auth.transport.requests
from google.oauth2.service_account import Credentials
from config import GOOGLE_SERVICE_ACCOUNT_FILE, SIM_OFFICES

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TAIPEI_TZ = datetime.timezone(datetime.timedelta(hours=8))
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"

_creds = None


def today_str():
    return datetime.datetime.now(TAIPEI_TZ).strftime("%Y/%m/%d")


def _get_access_token():
    global _creds
    if _creds is None:
        _creds = Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    if not _creds.valid:
        _creds.refresh(google.auth.transport.requests.Request())
    return _creds.token


def _headers():
    return {"Authorization": f"Bearer {_get_access_token()}", "Content-Type": "application/json"}


def _sheet_range(office: str, a1_range: str) -> str:
    sheet_name = SIM_OFFICES[office]["sheet_name"]
    return f"'{sheet_name}'!{a1_range}"


def _get_values(office: str, a1_range: str):
    spreadsheet_id = SIM_OFFICES[office]["spreadsheet_id"]
    encoded_range = urllib.parse.quote(_sheet_range(office, a1_range), safe="")
    url = f"{SHEETS_API}/{spreadsheet_id}/values/{encoded_range}"
    resp = requests.get(url, headers=_headers(), timeout=20)
    if not resp.ok:
        raise RuntimeError(f"讀取門號試算表失敗 [office={office}]: {resp.status_code} {resp.text[:200]}")
    return resp.json().get("values", [])


def _update_values(office: str, a1_range: str, values):
    spreadsheet_id = SIM_OFFICES[office]["spreadsheet_id"]
    encoded_range = urllib.parse.quote(_sheet_range(office, a1_range), safe="")
    url = f"{SHEETS_API}/{spreadsheet_id}/values/{encoded_range}"
    params = {"valueInputOption": "USER_ENTERED"}
    resp = requests.put(url, headers=_headers(), params=params, json={"values": values}, timeout=20)
    if not resp.ok:
        raise RuntimeError(f"寫入門號試算表失敗 [office={office}]: {resp.status_code} {resp.text[:200]}")
    return resp.json()


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

    values = _get_values(office, "A2:Z5000")
    for i, row in enumerate(values):
        row_num = i + 2  # 從第2列開始(第1列是標題)
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


_sheet_gid_cache = {}


def _get_sheet_gid(office: str):
    """輕量查詢分頁的數字 gid(只要 sheetId+title,不會像完整中繼資料那樣容易 500)"""
    if office in _sheet_gid_cache:
        return _sheet_gid_cache[office]
    spreadsheet_id = SIM_OFFICES[office]["spreadsheet_id"]
    sheet_name = SIM_OFFICES[office]["sheet_name"]
    url = f"{SHEETS_API}/{spreadsheet_id}"
    params = {"fields": "sheets.properties(sheetId,title)"}
    resp = requests.get(url, headers=_headers(), params=params, timeout=20)
    if not resp.ok:
        raise RuntimeError(f"查詢門號分頁資訊失敗 [office={office}]: {resp.status_code} {resp.text[:200]}")
    for sheet in resp.json().get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("title") == sheet_name:
            _sheet_gid_cache[office] = props.get("sheetId")
            return _sheet_gid_cache[office]
    raise RuntimeError(f"找不到分頁 [office={office}, sheet_name={sheet_name}]")


def _get_note(office: str, row: int, col_letter: str) -> str:
    """讀取單一儲存格目前的插入備註(note)內容"""
    spreadsheet_id = SIM_OFFICES[office]["spreadsheet_id"]
    range_a1 = _sheet_range(office, f"{col_letter}{row}")
    url = f"{SHEETS_API}/{spreadsheet_id}"
    params = {"ranges": range_a1, "fields": "sheets.data.rowData.values.note"}
    resp = requests.get(url, headers=_headers(), params=params, timeout=20)
    if not resp.ok:
        return ""
    try:
        row_data = resp.json()["sheets"][0]["data"][0].get("rowData", [])
        if not row_data:
            return ""
        values = row_data[0].get("values", [])
        if not values:
            return ""
        return (values[0].get("note") or "").rstrip()
    except (KeyError, IndexError):
        return ""


def _set_note(office: str, row: int, col_letter: str, note_text: str):
    """幫單一儲存格設定插入備註(note),不會動到儲存格本身顯示的文字"""
    spreadsheet_id = SIM_OFFICES[office]["spreadsheet_id"]
    gid = _get_sheet_gid(office)
    col_idx = _col_to_index(col_letter)
    body = {
        "requests": [
            {
                "updateCells": {
                    "range": {
                        "sheetId": gid,
                        "startRowIndex": row - 1,
                        "endRowIndex": row,
                        "startColumnIndex": col_idx - 1,
                        "endColumnIndex": col_idx,
                    },
                    "rows": [{"values": [{"note": note_text}]}],
                    "fields": "note",
                }
            }
        ]
    }
    url = f"{SHEETS_API}/{spreadsheet_id}:batchUpdate"
    resp = requests.post(url, headers=_headers(), json=body, timeout=20)
    if not resp.ok:
        raise RuntimeError(f"寫入門號備註失敗 [office={office}]: {resp.status_code} {resp.text[:200]}")


def set_note_display_text(office: str, row: int, text: str):
    """
    直接覆寫備註欄「儲存格本身顯示的文字」(跟 _set_note / append_sim_note
    操作的 hover 插入備註是兩件事,互不影響)。
    用在需要讓人一眼從表格上就看到目前狀態文字的情境。
    """
    columns = SIM_OFFICES[office]["columns"]
    col = columns["note"]
    _update_values(office, f"{col}{row}", [[text]])


def update_sim_fields(office: str, row: int, fields: dict):
    """一次更新多個欄位(例如 {'name': '小美', 'type': '公務機'})"""
    columns = SIM_OFFICES[office]["columns"]
    for field_key, value in fields.items():
        col = columns[field_key]
        _update_values(office, f"{col}{row}", [[value]])


def append_sim_note(office: str, row: int, text: str):
    """
    在附註欄的「插入備註」(hover 顯示的 note)累加一行「日期 說明」,保留歷史紀錄,
    不會覆蓋或動到儲存格本身顯示的文字。
    """
    columns = SIM_OFFICES[office]["columns"]
    col = columns["note"]
    existing = _get_note(office, row, col)
    new_line = f"{today_str()} {text}"
    combined = f"{existing}\n{new_line}" if existing else new_line
    _set_note(office, row, col, combined)
    return combined

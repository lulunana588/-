# sheets_service.py
# 封裝所有跟 Google Sheets 直接讀寫的邏輯（用服務帳戶 gspread，不用 GAS）

import re
import datetime
import gspread
from gspread.utils import rowcol_to_a1
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


# ---------------------------------------------------------
# 桶裝水 - 個別地點分頁（逐筆記錄：日期/備註/加值桶數/扣除桶數/剩餘數量）
# ---------------------------------------------------------

# 總覽表的「地點」欄位若帶有廠商後綴，代表該地點分頁裡有兩組並排的記錄區塊
# 比對到「水寶貝」就用右邊區塊（G欄開始），其餘（含華生／無後綴）都用左邊區塊（A欄開始）
_SUPPLIER_SUFFIX_RE = re.compile(r"^(?P<base>.*?)[（(](?P<supplier>[^（()）]+)[）)]$")

_LOG_COLS = ("日期", "備註", "加值桶數", "扣除桶數", "剩餘數量")  # 每個區塊固定5欄


def _resolve_detail_target(location_name: str):
    """回傳 (個別分頁標題, 區塊起始欄號)。區塊起始欄號 1=A欄, 7=G欄"""
    m = _SUPPLIER_SUFFIX_RE.match(location_name.strip())
    if not m:
        return location_name.strip(), 1
    base = m.group("base").strip()
    supplier = m.group("supplier").strip()
    col_start = 7 if "水寶貝" in supplier else 1
    return base, col_start


def get_water_detail_worksheet(tab_title: str):
    sh = get_client().open_by_key(config.WATER_SHEET_ID)
    return sh.worksheet(tab_title)


def append_water_log(location_name: str, delta: int, note: str = ""):
    """
    在該地點的個別分頁新增一筆逐日記錄，比照人工登記的格式。
    回傳 dict：{tab, new_balance}
    """
    tab_title, col_start = _resolve_detail_target(location_name)
    ws = get_water_detail_worksheet(tab_title)
    values = ws.get_all_values()

    # 找到這個區塊的標題列（該欄第一格內容為「日期」的那一列）
    header_idx = None
    for i, row in enumerate(values):
        cell = row[col_start - 1] if len(row) >= col_start else ""
        if cell.strip() == "日期":
            header_idx = i
            break
    if header_idx is None:
        raise RuntimeError(f"在分頁「{tab_title}」找不到「日期」標題欄，請確認表格式未被改動")

    balance_col = col_start + 4  # 剩餘數量欄，相對日期欄 +4

    # 找這個區塊目前最後一筆的「剩餘數量」，當作這次加減的起始庫存
    prev_balance = 0
    last_filled_idx = header_idx
    for i in range(header_idx + 1, len(values)):
        row = values[i]
        date_cell = row[col_start - 1] if len(row) >= col_start else ""
        if not date_cell.strip():
            break
        last_filled_idx = i
        bal_cell = row[balance_col - 1] if len(row) >= balance_col else ""
        if bal_cell.strip():
            try:
                prev_balance = int(bal_cell.strip())
            except ValueError:
                pass

    target_row = last_filled_idx + 2 if last_filled_idx > header_idx else header_idx + 2
    # (+2 是因為 header_idx / last_filled_idx 是 0-indexed，且要寫在下一列)

    new_balance = max(0, prev_balance + delta)
    # 送水（出庫）時加值桶數掛0；儲值（入庫）時扣除桶數掛0——不留空白，方便閱讀與後續加總計算
    # （2026/07/09 修正：原本用 "" 空字串，改成明確填 0）
    add_qty = delta if delta > 0 else 0
    minus_qty = -delta if delta < 0 else 0

    start_cell = rowcol_to_a1(target_row, col_start)
    end_cell = rowcol_to_a1(target_row, col_start + 4)
    ws.update(
        f"{start_cell}:{end_cell}",
        [[today_str(), note, add_qty, minus_qty, new_balance]],
    )

    return {"tab": tab_title, "new_balance": new_balance}


def record_water_transaction(loc: dict, delta: int, note: str = ""):
    """
    同時更新①總覽分頁的目前庫存 ②該地點個別分頁的逐筆記錄。
    loc 是 list_water_locations() 回傳的其中一筆（需要 row / location）。
    """
    summary_result = update_water_stock(loc["row"], delta)
    try:
        detail_result = append_water_log(loc["location"], delta, note=note)
    except Exception:
        # 總覽已經寫成功，個別分頁若失敗仍要讓使用者知道總覽有更新，
        # 但這個例外要往上拋，讓呼叫端可以分開告知使用者
        raise
    summary_result["detail_tab"] = detail_result["tab"]
    summary_result["detail_balance"] = detail_result["new_balance"]
    return summary_result


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


def add_payment_record(
    name: str,
    amount: str,
    submit_date: str,
    progress: str,
    status: str = "待付",
    paid_date: str = "",
    note: str = "",
    operator: str = "",
):
    """
    新增一筆款項紀錄。編號自動遞增。
    欄位順序：編號/送件日期/操作人/款項名稱/金額/進度/付款狀態/實付日期/備註
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
        f"A{target_row}:I{target_row}",
        [[new_id, submit_date, operator, name, amount, progress, status, paid_date, note]],
    )
    return {
        "id": new_id,
        "row": target_row,
        "name": name,
        "amount": amount,
        "submit_date": submit_date,
        "progress": progress,
        "status": status,
        "paid_date": paid_date,
    }


def find_near_pending_payment(name: str, amount: str, tolerance: int):
    """
    找出款項名稱完全相同、金額在誤差範圍內（但不完全相同）、且還沒付的既有列。
    用於：金額有一點誤差時，先跳出來給使用者確認是不是同一筆，而不是直接自動合併或誤判成新的一筆。
    回傳 [{row, amount}, ...]
    """
    ws = get_payment_worksheet()
    all_values = ws.get_all_values()
    header_idx = _payment_header_row_idx(all_values)

    try:
        target = int(amount.replace(",", "").strip())
    except ValueError:
        return []

    matches = []
    for i in range(header_idx + 1, len(all_values)):
        row = all_values[i]
        if not row or not row[0].strip():
            continue
        row_name = row[3].strip() if len(row) > 3 else ""
        row_amount_raw = row[4].strip().replace(",", "") if len(row) > 4 else ""
        row_status = row[6].strip() if len(row) > 6 else ""
        if row_name != name or row_status == "已付":
            continue
        try:
            row_amount = int(row_amount_raw)
        except ValueError:
            continue
        if row_amount == target:
            continue  # 完全相同的交給 find_pending_payment_exact 處理，這裡只找「有誤差」的
        if abs(row_amount - target) <= tolerance:
            matches.append({"row": i + 1, "amount": row_amount})
    return matches


def get_pending_payment_index():
    """
    取得目前所有「非已付」款項的索引快照，給批次處理比對用。
    只在批次開始前讀一次，避免同一批裡有重複名稱+金額的項目時，
    後面那筆誤判成在編輯前面那筆剛新增的（應該各自成立一筆）。
    回傳 [{row, name, amount}, ...]（amount 是 int）
    """
    ws = get_payment_worksheet()
    all_values = ws.get_all_values()
    header_idx = _payment_header_row_idx(all_values)

    index = []
    for i in range(header_idx + 1, len(all_values)):
        row = all_values[i]
        if not row or not row[0].strip():
            continue
        status = row[6].strip() if len(row) > 6 else ""
        if status == "已付":
            continue
        name = row[3].strip() if len(row) > 3 else ""
        amount_raw = row[4].strip().replace(",", "") if len(row) > 4 else ""
        try:
            amount = int(amount_raw)
        except ValueError:
            continue
        index.append({"row": i + 1, "name": name, "amount": amount})
    return index


def find_pending_payment_exact(name: str, amount: str):
    """
    找出款項名稱、金額都完全相同，且目前狀態不是「已付」的既有列。
    用於快速指令判斷：同一筆款項再送一次時，應該編輯既有的，而不是新增重複的一筆。
    回傳符合的列號清單（1-indexed），可能是 0/1/多筆。
    """
    ws = get_payment_worksheet()
    all_values = ws.get_all_values()
    header_idx = _payment_header_row_idx(all_values)

    amount = amount.replace(",", "").strip()
    matches = []
    for i in range(header_idx + 1, len(all_values)):
        row = all_values[i]
        if not row or not row[0].strip():
            continue
        row_name = row[3].strip() if len(row) > 3 else ""
        row_amount = row[4].strip().replace(",", "") if len(row) > 4 else ""
        row_status = row[6].strip() if len(row) > 6 else ""
        if row_name == name and row_amount == amount and row_status != "已付":
            matches.append(i + 1)
    return matches


def update_payment_fields(
    row: int,
    progress: str = None,
    status: str = None,
    paid_date: str = None,
    note: str = None,
    operator: str = None,
):
    """
    更新既有款項列的操作人/進度/付款狀態/實付日期/備註（只更新有提供值的欄位，其餘保留原值）。
    """
    ws = get_payment_worksheet()
    current = ws.row_values(row)

    cur_operator = current[2] if len(current) > 2 else ""
    cur_progress = current[5] if len(current) > 5 else ""
    cur_status = current[6] if len(current) > 6 else ""
    cur_paid_date = current[7] if len(current) > 7 else ""
    cur_note = current[8] if len(current) > 8 else ""

    new_operator = operator if operator else cur_operator
    new_progress = progress if progress else cur_progress
    new_status = status if status else cur_status
    new_paid_date = paid_date if paid_date is not None and paid_date != "" else cur_paid_date
    new_note = note if note else cur_note

    if operator:
        ws.update(f"C{row}", [[new_operator]])
    ws.update(f"F{row}:I{row}", [[new_progress, new_status, new_paid_date, new_note]])

    return {
        "id": current[0] if current else "",
        "name": current[3] if len(current) > 3 else "",
        "amount": current[4] if len(current) > 4 else "",
        "progress": new_progress,
        "status": new_status,
        "paid_date": new_paid_date,
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
        name = row[3].strip() if len(row) > 3 else ""
        if query == row_id:
            results.insert(0, {"row": i + 1, "values": row})  # 精準比對排最前面
        elif query and query in name:
            results.append({"row": i + 1, "values": row})
        if len(results) >= limit:
            break
    return results[:limit]


def mark_payment_paid(row: int, paid_date: str = None, note: str = None, operator: str = None):
    """把付款狀態改為已付，並帶入實付日期；若有給 note 則附加到備註欄；operator 則寫入操作人欄（覆蓋）"""
    ws = get_payment_worksheet()
    paid_date = paid_date or today_str()
    current = ws.row_values(row)
    old_note = current[8] if len(current) > 8 else ""

    ws.update(f"G{row}:H{row}", [["已付", paid_date]])
    if note:
        combined_note = f"{old_note}；{note}" if old_note else note
        ws.update(f"I{row}", [[combined_note]])
    if operator:
        ws.update(f"C{row}", [[operator]])

    return {
        "id": current[0] if current else "",
        "name": current[3] if len(current) > 3 else "",
        "amount": current[4] if len(current) > 4 else "",
        "paid_date": paid_date,
    }


# 「已付」但進度還卡在候補發票，也要算進追蹤清單裡
_NEEDS_TRACKING_EVEN_IF_PAID_PROGRESS = "已提交請款單候補發票"


def get_pending_payments():
    """
    取得所有「需要追蹤」的款項：
      - 付款狀態是「待付」的，全部都算
      - 付款狀態是「已付」但進度還是「已提交請款單候補發票」的，也算
    其餘（已付且進度已完成）不算。
    回傳 {count, total, items: [{id, name, amount, status, progress}, ...]}
    """
    ws = get_payment_worksheet()
    all_values = ws.get_all_values()
    header_idx = _payment_header_row_idx(all_values)

    items = []
    for row in all_values[header_idx + 1 :]:
        if not row or not row[0].strip():
            continue
        status = row[6].strip() if len(row) > 6 else ""
        progress = row[5].strip() if len(row) > 5 else ""

        needs_tracking = (status != "已付") or (
            progress == _NEEDS_TRACKING_EVEN_IF_PAID_PROGRESS
        )
        if not needs_tracking:
            continue

        name = row[3].strip() if len(row) > 3 else ""
        amount_raw = row[4].strip() if len(row) > 4 else "0"
        try:
            amount = int(amount_raw.replace(",", ""))
        except ValueError:
            amount = 0

        items.append(
            {
                "id": row[0].strip(),
                "name": name,
                "amount": amount,
                "status": status or "（未填）",
                "progress": progress or "（未填）",
            }
        )

    total = sum(i["amount"] for i in items)
    return {"count": len(items), "total": total, "items": items}


# =========================================================
# 綜辦文件繳回追蹤表
# =========================================================

_DOC_TRACKING_STATUSES = ("待回綜辦", "待繳回綜辦")


def get_doc_worksheet():
    return _open_worksheet(config.DOC_SHEET_ID, config.DOC_GID)


def _doc_header_row_idx(all_values):
    for i, row in enumerate(all_values):
        if row and row[0].strip() == "收到日期":
            return i
    raise RuntimeError("找不到綜辦文件表的標題列（收到日期/公司/...），請確認表格式未被改動")


def get_pending_docs():
    """
    取得所有「狀態」欄是「待繳回綜辦」或「已繳回綜辦」的文件列
    （「已交至財務部」跟其他狀態不算）。
    回傳 {count, items: [{received_date, company, doc_type, detail, status}, ...]}
    """
    ws = get_doc_worksheet()
    all_values = ws.get_all_values()
    header_idx = _doc_header_row_idx(all_values)

    items = []
    for row in all_values[header_idx + 1 :]:
        if not row or not row[0].strip():
            continue
        status = row[4].strip() if len(row) > 4 else ""
        if status not in _DOC_TRACKING_STATUSES:
            continue

        items.append(
            {
                "received_date": row[0].strip(),
                "company": row[1].strip() if len(row) > 1 else "",
                "doc_type": row[2].strip() if len(row) > 2 else "",
                "detail": row[3].strip() if len(row) > 3 else "",
                "status": status,
            }
        )

    return {"count": len(items), "items": items}

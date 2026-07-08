# fix_water_stock.py
# 一次性修復腳本：修正 2026/07/08 因先前 bug 造成的錯誤桶裝水紀錄
#
# 會做的事：
# 1. 讀取「運營中心南京辦」「客服中心松山辦」兩個分頁，
#    把所有日期是 2026/07/08 的列（不管有幾筆重複/錯誤）全部清空，
#    只留一筆正確的：加值桶數空白、扣除桶數=10、剩餘數量=正確值
# 2. 同步把總覽表這兩個地點的「目前庫存」欄改成正確值
#
# 正確值：
#   運營中心南京辦：07/02 剩餘75 → 07/08 扣10 → 65
#   客服中心松山辦：07/01 剩餘180 → 07/08 扣10 → 170
#
# 執行方式：放在 /root/luna_bot/ 目錄下（跟 main_bot.py 同一層），執行：
#   python3 fix_water_stock.py
#
# 這支腳本只會執行「一次」，改完之後刪掉即可，不需要保留或排程。

import sheets_service as sheets

FIXES = [
    {"location": "運營中心南京辦", "correct_balance": 65},
    {"location": "客服中心松山辦", "correct_balance": 170},
]

TARGET_DATE = "2026/07/08"


def fix_detail_tab(location_name: str, correct_balance: int):
    tab_title, col_start = sheets._resolve_detail_target(location_name)
    ws = sheets.get_water_detail_worksheet(tab_title)
    values = ws.get_all_values()

    header_idx = None
    for i, row in enumerate(values):
        cell = row[col_start - 1] if len(row) >= col_start else ""
        if cell.strip() == "日期":
            header_idx = i
            break
    if header_idx is None:
        raise RuntimeError(f"在分頁「{tab_title}」找不到「日期」標題欄")

    # 找出所有日期是 TARGET_DATE 的列（可能有多筆重複/錯誤）
    target_rows = []
    for i in range(header_idx + 1, len(values)):
        row = values[i]
        date_cell = row[col_start - 1] if len(row) >= col_start else ""
        if date_cell.strip() == TARGET_DATE:
            target_rows.append(i + 1)  # 1-indexed
        elif not date_cell.strip():
            break

    if not target_rows:
        print(f"  [{tab_title}] 找不到 {TARGET_DATE} 的紀錄，略過")
        return

    print(f"  [{tab_title}] 找到 {len(target_rows)} 筆 {TARGET_DATE} 的重複/錯誤紀錄：列 {target_rows}")

    first_row = target_rows[0]
    extra_rows = target_rows[1:]

    # 把第一筆改成正確值：加值空白、扣除10、剩餘正確值
    start_col = col_start
    end_col = col_start + 4
    from gspread.utils import rowcol_to_a1
    start_cell = rowcol_to_a1(first_row, start_col)
    end_cell = rowcol_to_a1(first_row, end_col)
    ws.update(f"{start_cell}:{end_cell}", [[TARGET_DATE, "", "", 10, correct_balance]])
    print(f"  [{tab_title}] 第 {first_row} 列已更新為：扣除10、剩餘{correct_balance}")

    # 其餘重複列整列清空（由下往上刪，避免行號錯位）
    for r in sorted(extra_rows, reverse=True):
        clear_start = rowcol_to_a1(r, start_col)
        clear_end = rowcol_to_a1(r, end_col)
        ws.update(f"{clear_start}:{clear_end}", [["", "", "", "", ""]])
        print(f"  [{tab_title}] 第 {r} 列（重複錯誤紀錄）已清空")


def fix_overview(location_name: str, correct_balance: int):
    locations = sheets.list_water_locations()
    match = next((loc for loc in locations if loc["location"] == location_name), None)
    if not match:
        print(f"  [總覽表] 找不到地點「{location_name}」，請確認名稱是否完全一致")
        return

    row = match["row"]
    new_status = sheets.compute_water_status(correct_balance)
    ws = sheets.get_water_worksheet()
    ws.update(f"C{row}:D{row}", [[TARGET_DATE, correct_balance]])
    ws.update(f"F{row}", [[new_status]])
    print(f"  [總覽表] 第 {row} 列（{location_name}）目前庫存已改為 {correct_balance}，狀態：{new_status}")


def main():
    for fix in FIXES:
        loc = fix["location"]
        bal = fix["correct_balance"]
        print(f"\n=== 修正「{loc}」 ===")
        fix_detail_tab(loc, bal)
        fix_overview(loc, bal)
    print("\n全部修正完成！")


if __name__ == "__main__":
    main()

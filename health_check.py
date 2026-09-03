# health_check.py
# 鵝鵝bot（luna-bot）功能性健康檢查
#
# 跟單純看 systemctl status（process-level）不一樣，這支腳本會「真的」去測：
#   1. Google Sheets 服務帳戶連線是否正常
#   2. 桶裝水總覽表、款項追蹤表的標題列是否都找得到（表格結構沒有被誤改）
#   3. 每個地點「總覽表庫存」跟「分頁最後一筆剩餘數量」是否一致
#      （這項就是宏國辦那次錯位問題的事後對帳版本，讓同類問題以後會被主動抓到）
#   4. Telegram Bot API 連線是否正常
#
# 平常安靜跑，只有真的異常時才會推播 Telegram 通知（發到 config.REMINDER_CHAT_ID），
# 不會像 diary-bot 的 api_health_test.py 一樣每次都回報「一切正常」洗版。
#
# 建議透過 cron 定期執行（例如每天早上跑一次），執行方式：
#   cd /root/luna_bot && python3 health_check.py

import sys
import traceback
import requests

import config
import sheets_service as sheets


def send_alert(text: str):
    """發送 Telegram 通知到 REMINDER_CHAT_ID，走 Bot API 的 sendMessage，不依賴 python-telegram-bot 的 async 機制，
    這樣這支腳本可以用最單純的方式跑 cron，不用管事件迴圈。"""
    if not config.REMINDER_CHAT_ID:
        print("REMINDER_CHAT_ID 未設定，無法發送異常通知，僅印出以下內容：")
        print(text)
        return
    try:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": config.REMINDER_CHAT_ID, "text": text}, timeout=10)
    except Exception:
        print("發送異常通知本身失敗，請人工檢查：")
        print(traceback.format_exc())


def check_sheets_connection():
    """檢查1：Google Sheets 服務帳戶連線是否正常"""
    try:
        sheets.get_client()
        return True, ""
    except Exception as e:
        return False, f"Google Sheets 連線失敗：{e}"


def check_water_overview_structure():
    """檢查2a：桶裝水總覽表的標題列是否找得到"""
    try:
        locations = sheets.list_water_locations()
        if not locations:
            return False, "桶裝水總覽表讀取成功，但沒有找到任何地點資料"
        return True, f"共 {len(locations)} 個地點"
    except Exception as e:
        return False, f"桶裝水總覽表讀取失敗：{e}"


def check_payment_structure():
    """檢查2b：款項追蹤表的標題列是否找得到"""
    try:
        summary = sheets.get_pending_payments()
        return True, f"目前 {summary['count']} 筆待追蹤款項"
    except Exception as e:
        return False, f"款項追蹤表讀取失敗：{e}"


def check_water_consistency():
    """
    檢查3：每個地點「總覽表庫存」跟「分頁最後一筆剩餘數量」是否一致。
    不一致不代表機器人壞掉，比較常見的原因是人工在分頁補登記時忘記同步改總覽表，
    或分頁中間出現過我們之前修過的那類異常。列出來讓 Luna 知道要去核對，不會自動改資料。
    """
    mismatches = []
    try:
        locations = sheets.list_water_locations()
    except Exception as e:
        return False, f"無法讀取總覽表，略過一致性檢查：{e}", []

    for loc in locations:
        try:
            detail_balance = sheets.get_detail_last_balance(loc["location"])
        except Exception as e:
            mismatches.append(f"「{loc['location']}」讀取分頁失敗：{e}")
            continue
        if detail_balance is None:
            continue  # 分頁還沒有任何資料列，不算異常，跳過
        if detail_balance != loc["stock"]:
            mismatches.append(
                f"「{loc['location']}」總覽表顯示 {loc['stock']} 桶，"
                f"分頁最後一筆卻是 {detail_balance} 桶"
            )

    if mismatches:
        return False, "\n".join(mismatches), mismatches
    return True, f"{len(locations)} 個地點全部一致", []


def check_telegram_api():
    """檢查4：Telegram Bot API 連線是否正常"""
    try:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getMe"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if data.get("ok"):
            return True, f"Bot：@{data['result'].get('username', '未知')}"
        return False, f"Telegram API 回應異常：{data}"
    except Exception as e:
        return False, f"Telegram API 連線失敗：{e}"


def main():
    problems = []

    checks = [
        ("Google Sheets 連線", check_sheets_connection),
        ("桶裝水總覽表結構", check_water_overview_structure),
        ("款項追蹤表結構", check_payment_structure),
        ("Telegram Bot API 連線", check_telegram_api),
    ]

    for name, fn in checks:
        ok, detail = fn()
        status = "✅" if ok else "❌"
        print(f"{status} {name}：{detail}")
        if not ok:
            problems.append(f"❌ {name}：{detail}")

    # 一致性檢查回傳格式不同（多一個 mismatches 清單），分開處理
    ok, detail, mismatches = check_water_consistency()
    status = "✅" if ok else "⚠️"
    print(f"{status} 總覽表/分頁一致性：{detail}")
    if not ok:
        problems.append(f"⚠️ 總覽表/分頁對不上：\n{detail}")

    if problems:
        alert_text = (
            "🚨 鵝鵝bot 健康檢查發現異常：\n\n" + "\n\n".join(problems) +
            "\n\n（此為自動偵測，異常項目不會自動修正，麻煩人工確認）"
        )
        send_alert(alert_text)
        print("\n已發送異常通知。")
        sys.exit(1)
    else:
        print("\n全部檢查通過，一切正常（不會發送通知）。")
        sys.exit(0)


if __name__ == "__main__":
    main()

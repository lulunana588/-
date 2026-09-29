"""VVS小秘書｜每日體重提醒（cron 每 5 分鐘執行）

每個人依自己設定的時間提醒（預設 09:00，可傳「提醒 07:30」「提醒 關閉」修改）；
當天已經記錄、或今天已經提醒過，就不會再推播。
"""
import vvs_config as cfg
import vvs_db as db
import vvs_line as line
import vvs_weight as weight


def main():
    db.init()
    now = weight.now_tpe()
    for uid in cfg.OWNER_USER_IDS:
        msg = weight.reminder_due(uid, now)
        if msg:
            ok = line.push(uid, [msg], quick=weight.QUICK)
            print(f'{now:%Y-%m-%d %H:%M} remind {uid[:6]}… {"OK" if ok else "FAIL"}', flush=True)


if __name__ == '__main__':
    main()

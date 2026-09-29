"""VVS小秘書｜每週摘要（cron 週日晚上執行）

每個人只收到自己的摘要；本週沒有紀錄的人不推播。
"""
from datetime import datetime

import vvs_config as cfg
import vvs_db as db
import vvs_line as line
import vvs_weight as weight


def main():
    db.init()
    for uid in cfg.OWNER_USER_IDS:
        msg = weight.weekly_summary(uid)
        if msg:
            ok = line.push(uid, [msg], quick=weight.QUICK)
            print(f'{datetime.now(cfg.TZ):%Y-%m-%d %H:%M} weekly {uid[:6]}… {"OK" if ok else "FAIL"}', flush=True)


if __name__ == '__main__':
    main()

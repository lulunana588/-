"""VVS小秘書｜每週／每月摘要（cron）

  python vvs_weekly.py            週日晚上：本週摘要
  python vvs_weekly.py --monthly  每月 1 號：上個月摘要
每個人只收到自己的摘要；期間內沒有紀錄的人不推播。
"""
import sys
from datetime import datetime

import vvs_config as cfg
import vvs_db as db
import vvs_jobs as jobs
import vvs_line as line
import vvs_weight as weight


def main(monthly=False):
    db.init()
    kind = 'monthly' if monthly else 'weekly'
    for uid in cfg.OWNER_USER_IDS:
        msg = weight.monthly_summary(uid) if monthly else weight.weekly_summary(uid)
        if msg:
            ok = line.push(uid, [msg], quick=weight.QUICK)
            print(f'{datetime.now(cfg.TZ):%Y-%m-%d %H:%M} {kind} {uid[:6]}… {"OK" if ok else "FAIL"}', flush=True)
    jobs.beat(kind)


if __name__ == '__main__':
    main(monthly='--monthly' in sys.argv)

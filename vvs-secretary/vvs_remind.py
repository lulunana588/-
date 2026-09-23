"""VVS小秘書｜每日體重提醒（cron 執行，今天還沒記錄才推播）"""
import vvs_config as cfg
import vvs_db as db
import vvs_line as line
import vvs_weight as weight


def main():
    db.init()
    for uid in cfg.OWNER_USER_IDS:
        msg = weight.reminder_for(uid)
        if msg:
            ok = line.push(uid, [msg], quick=weight.QUICK)
            print(f'{weight.now_tpe():%Y-%m-%d %H:%M} push {uid[:6]}… {"OK" if ok else "FAIL"}')


if __name__ == '__main__':
    main()

"""VVS小秘書｜部署用的整體自我測試（vvs_deploy.sh 第 5 關呼叫）

以後新增模組的自我測試，只要加進下面的 TESTS，透過一般部署就會生效，
不用再改 vvs_deploy.sh。
"""
import sys
import traceback

TESTS = [
    ('體重計算', 'vvs_weight', 'selftest'),
    ('排程監控', 'vvs_jobs', 'selftest'),
    ('備份演練', 'vvs_backup', 'drill_selftest'),
]


def main():
    done, failed = [], []
    for label, mod, fn in TESTS:
        try:
            ok = getattr(__import__(mod), fn)()
            (done if ok is not False else failed).append(label)
        except Exception:
            failed.append(label)
            traceback.print_exc(limit=3)
    print('｜'.join([f'{x} ✓' for x in done] + [f'{x} ✗' for x in failed]))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())

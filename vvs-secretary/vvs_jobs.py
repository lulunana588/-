"""VVS小秘書｜排程心跳、排程監控、紀錄檔輪替

心跳：每個排程跑完都呼叫 beat('名稱')，在 data/beats/ 留下時間戳（只記時間，不含任何使用者資料）。
監控：vvs_healthcheck.py 每小時呼叫 late_jobs()，有排程逾時就通知管理者。
      健康檢查本身則由每 5 分鐘的提醒排程反過來看守（watch_health）。
輪替：每月第一次執行時，把 data/*.log 改名成 *.log.YYYY-MM，只保留最近 3 個月；
      privacy_events.log 不改名，改成刪掉 90 天前的行（隱私稽核要讀近 7 天）。
"""
import os
import time
from datetime import datetime, timedelta

import vvs_config as cfg

BEAT_DIR = cfg.DATA_DIR / 'beats'
H = 3600

# 名稱：(顯示名稱, 最長容許間隔秒數, 應有頻率說明)
JOBS = {
    'remind':  ('體重提醒', 20 * 60, '每 5 分鐘'),
    'backup':  ('每日備份', 26 * H, '每天 03:00'),
    'privacy': ('隱私稽核', 26 * H, '每天 04:00'),
    'weekly':  ('週摘要', 7 * 24 * H + 3 * H, '每週日 21:00'),
    'monthly': ('月摘要', 31 * 24 * H + 3 * H, '每月 1 號 21:00'),
    'drill':   ('備份還原演練', 31 * 24 * H + 3 * H, '每月初'),
}
HEALTH_MAX = 3 * H                 # 健康檢查每小時跑，超過 3 小時沒跑就由提醒排程通知
WATCH_EVERY = 6 * H                # 看守通知最多每 6 小時一次
KEEP_MONTHS = 3
EVENTS_KEEP_DAYS = 90
EVENTS_LOG = 'privacy_events.log'


def _dir():
    BEAT_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(BEAT_DIR, 0o700)
    since = BEAT_DIR / '_since'
    if not since.exists():
        since.write_text(str(time.time()), encoding='utf-8')
    return BEAT_DIR


def beat(name):
    """排程跑完呼叫；失敗也不影響排程本身"""
    try:
        p = _dir() / name
        p.write_text(f'{datetime.now(cfg.TZ).isoformat(timespec="seconds")}\n', encoding='utf-8')
        os.chmod(p, 0o600)
    except OSError:
        pass


def last_beat(name):
    p = BEAT_DIR / name
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def _since():
    try:
        return float((_dir() / '_since').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return time.time()


def _age(sec):
    if sec >= 2 * 24 * H:
        return f'{sec / 86400:.0f} 天'
    if sec >= H:
        return f'{sec / H:.0f} 小時'
    return f'{sec / 60:.0f} 分鐘'


def late_jobs(now=None):
    """回傳逾時排程的說明列表；全部準時回傳 []"""
    now = now or time.time()
    since = _since()
    out = []
    for name, (label, max_age, freq) in JOBS.items():
        t = last_beat(name)
        if t is None:
            if now - since > max_age:
                out.append(f'{label}從監控開始後一次都沒跑（應{freq}）')
        elif now - t > max_age:
            out.append(f'{label}已 {_age(now - t)}沒跑（應{freq}）')
    return out


def watch_health(alert, now=None):
    """由提醒排程呼叫：健康檢查超過 3 小時沒跑就通知（最多每 6 小時一次）"""
    now = now or time.time()
    t = last_beat('health') or _since()
    if now - t <= HEALTH_MAX:
        return False
    flag = _dir() / '_watch_alert'
    try:
        if now - flag.stat().st_mtime < WATCH_EVERY:
            return False
    except OSError:
        pass
    flag.write_text(str(now), encoding='utf-8')
    alert(f'🚨 小秘書排程監控\n每小時的健康檢查已 {_age(now - t)}沒跑，'
          f'其他排程有沒有準時也暫時無法確認。\n請到 VPS 執行：crontab -l 確認排程還在')
    return True


# ================= 紀錄檔輪替 =================

def monthly_due(name, now=None):
    """這個月還沒跑過 name 就回傳 True 並記下本月（每月只會回傳一次 True）"""
    now = now or datetime.now(cfg.TZ)
    cur = _month_key(now)
    p = _dir() / f'_month_{name}'
    try:
        if p.read_text(encoding='utf-8').strip() == cur:
            return False
    except OSError:
        pass
    p.write_text(cur, encoding='utf-8')
    return True


def _month_key(dt):
    return f'{dt.year:04d}-{dt.month:02d}'


def _months_before(dt, n):
    y, m = dt.year, dt.month - n
    while m <= 0:
        y, m = y - 1, m + 12
    return f'{y:04d}-{m:02d}'


def rotate_logs(now=None):
    """回傳 (改名數, 刪除數)。每月只會真正改名一次。"""
    now = now or datetime.now(cfg.TZ)
    cur = _month_key(now)
    marker = cfg.DATA_DIR / 'log_month'
    renamed = removed = 0
    try:
        prev = marker.read_text(encoding='utf-8').strip()
    except OSError:
        prev = ''
    if not prev:
        marker.write_text(cur, encoding='utf-8')        # 第一次啟用：只記錄月份
    elif prev != cur:
        for p in cfg.DATA_DIR.glob('*.log'):
            if p.name == EVENTS_LOG:
                continue
            dest = p.with_name(f'{p.name}.{prev}')
            if dest.exists():
                continue
            p.rename(dest)
            os.chmod(dest, 0o600)
            renamed += 1
        marker.write_text(cur, encoding='utf-8')

    oldest = _months_before(now, KEEP_MONTHS)            # 只留最近 3 個月的封存
    for p in cfg.DATA_DIR.glob('*.log.????-??'):
        if p.name[-7:] < oldest:
            p.unlink()
            removed += 1

    ev = cfg.DATA_DIR / EVENTS_LOG
    if ev.exists():
        cutoff = now - timedelta(days=EVENTS_KEEP_DAYS)
        lines = ev.read_text(encoding='utf-8').splitlines()
        keep = []
        for ln in lines:
            try:
                if datetime.fromisoformat(ln.split('\t')[0]) < cutoff:
                    continue
            except ValueError:
                pass
            keep.append(ln)
        if len(keep) != len(lines):
            ev.write_text(''.join(k + '\n' for k in keep), encoding='utf-8')
            removed += len(lines) - len(keep)
    return renamed, removed


def selftest():
    """健康檢查／部署用：在暫存目錄驗證心跳與輪替邏輯"""
    import tempfile
    from pathlib import Path
    global BEAT_DIR
    real_data, real_beat = cfg.DATA_DIR, BEAT_DIR
    with tempfile.TemporaryDirectory() as tmp:
        cfg.DATA_DIR, BEAT_DIR = Path(tmp), Path(tmp) / 'beats'
        try:
            t0 = time.time()
            assert late_jobs(t0) == []                                  # 剛啟用不誤報
            assert len(late_jobs(t0 + 40 * 24 * H)) == len(JOBS)        # 都沒跑要全抓到
            for n in JOBS:
                beat(n)
            assert late_jobs() == []
            assert any('體重提醒' in s for s in late_jobs(time.time() + 30 * 60))
            sent = []
            assert watch_health(sent.append, t0 + 4 * H) and sent        # 健康檢查沒跑要通知
            assert not watch_health(sent.append, t0 + 5 * H)             # 6 小時內不重複

            d = Path(tmp)
            (d / 'a.log').write_text('x\n')
            (d / 'a.log.2000-01').write_text('old\n')
            jan = datetime(2026, 1, 15, tzinfo=cfg.TZ)
            (d / EVENTS_LOG).write_text(f'{(jan - timedelta(days=100)).isoformat()}\tgroup\tx\n'
                                        f'{(jan - timedelta(days=1)).isoformat()}\tgroup\ty\n')
            rotate_logs(jan)                                             # 第一次只記月份
            assert (d / 'a.log').exists()
            r, _ = rotate_logs(datetime(2026, 2, 1, tzinfo=cfg.TZ))
            assert r == 1 and (d / 'a.log.2026-01').exists() and not (d / 'a.log').exists()
            assert not (d / 'a.log.2000-01').exists()                    # 超過 3 個月刪除
            assert (d / EVENTS_LOG).read_text().count('\n') == 1         # 事件只留 90 天內
            rotate_logs(datetime(2026, 5, 1, tzinfo=cfg.TZ))
            assert not (d / 'a.log.2026-01').exists()                    # 1 月在 5 月時淘汰
        finally:
            cfg.DATA_DIR, BEAT_DIR = real_data, real_beat
    return True


if __name__ == '__main__':
    import sys
    if '--status' in sys.argv:
        for n, (label, _, freq) in JOBS.items():
            t = last_beat(n)
            print(f'{label:<6} {freq:<12} 上次：' +
                  (datetime.fromtimestamp(t, cfg.TZ).strftime('%m/%d %H:%M') if t else '尚無紀錄'))
        late = late_jobs()
        print('\n' + ('全部準時 ✅' if not late else '逾時：\n' + '\n'.join(late)))
    else:
        print('OK' if selftest() else 'FAIL')

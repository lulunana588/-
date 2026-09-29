"""VVS小秘書｜LINE Webhook 主程式（Flask + gunicorn，127.0.0.1:5000，Caddy 反向代理）"""
import base64
import hashlib
import hmac
import json
import logging
import re

from flask import Flask, abort, jsonify, request, send_from_directory

import vvs_config as cfg
import vvs_db as db
import vvs_line as line
import vvs_privacy as privacy
import vvs_weight as weight

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
log = logging.getLogger('vvs')

app = Flask(__name__)
db.init()

FALLBACK = ('目前小秘書只開放體重功能 🙂\n'
            '直接輸入體重即可，例如：84.5\n'
            '輸入「說明」看全部指令')
CHART_RE = re.compile(r'^[0-9a-f]{32}\.png$')


def valid_signature(body: bytes, sig: str) -> bool:
    if not cfg.LINE_SECRET or not sig:
        return False
    mac = hmac.new(cfg.LINE_SECRET.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(mac).decode(), sig)


@app.post('/callback')
def callback():
    body = request.get_data()
    if not valid_signature(body, request.headers.get('X-Line-Signature', '')):
        log.warning('簽章驗證失敗，拒絕請求')
        abort(400)
    for ev in json.loads(body).get('events', []):
        try:
            handle_event(ev)
        except Exception:
            log.exception('處理事件失敗：%s', ev.get('webhookEventId'))
    return 'OK'


def handle_event(ev):
    eid = ev.get('webhookEventId')
    if eid and not db.mark_event(eid):
        return  # LINE 重送的重複事件

    src = ev.get('source') or {}
    uid = src.get('userId')
    token = ev.get('replyToken')

    # 隱私保護：只在一對一聊天運作。被加進群組／多人聊天室時，說明後立刻離開，
    # 避免任何人的體重數字出現在其他人看得到的地方。
    if src.get('type') != 'user':
        if ev.get('type') in ('join', 'message') and token:
            line.reply(token, [line.text('🔒 為了保護每個人的體重隱私，小秘書只在一對一聊天中使用，現在會離開這個群組。')])
        line.leave(src)
        gid = src.get('groupId') or src.get('roomId') or ''
        log.info('已離開非一對一聊天：%s', src.get('type'))
        privacy.record_event('group', gid)
        if ev.get('type') == 'join':
            privacy.alert_admin('🔒 隱私警報\n有人把小秘書加入了群組／聊天室，已自動離開，沒有透露任何資料。')
        return

    if not cfg.OWNER_USER_IDS:
        if token and uid:
            line.reply(token, [line.text(
                f'尚未設定使用者。\n你的 userId 是：\n{uid}\n\n'
                '請填入 .env 的 OWNER_USER_IDS 後重啟服務。')])
        return
    if uid not in cfg.OWNER_USER_IDS:
        # 非白名單：不回覆任何內容；每個陌生帳號每天通知管理者一次
        if uid and ev.get('type') in ('message', 'follow'):
            privacy.record_event('stranger', uid)
            today = f'{weight.now_tpe():%Y-%m-%d}'
            if db.get_setting('_stranger_alert', uid) != today:
                db.set_setting('_stranger_alert', uid, today)
                privacy.alert_admin(f'👀 隱私警報\n有不在白名單的帳號（{uid[:8]}…）傳訊息給小秘書，已忽略，沒有回覆任何資料。')
        return

    if ev.get('type') == 'follow':
        line.reply(token, [weight.help_msg()], quick=weight.QUICK)
        return

    msg = ev.get('message') or {}
    if ev.get('type') != 'message' or msg.get('type') != 'text':
        return

    text = (msg.get('text') or '').strip()
    if text in ('隱私檢查', '隱私') and uid == privacy.ADMIN:
        line.reply(token, [line.text(privacy.format_report(privacy.run_audit()))], quick=weight.QUICK)
        return

    msgs = weight.handle(uid, text)
    # 之後新增的模組（AI、天氣、匯率…）接在這裡：msgs = msgs or other.handle(...)
    if msgs is None:
        msgs = [line.text(FALLBACK)]
    line.reply(token, msgs, quick=weight.QUICK)


@app.get('/charts/<name>')
def chart(name):
    if not CHART_RE.match(name):
        abort(404)
    return send_from_directory(cfg.CHART_DIR, name, mimetype='image/png', max_age=86400)


@app.get('/health')
def health():
    checks = {'db': False, 'weight': False,
              'secret': bool(cfg.LINE_SECRET), 'token': bool(cfg.LINE_TOKEN),
              'owner': bool(cfg.OWNER_USER_IDS)}
    try:
        db.ping()
        checks['db'] = True
    except Exception:
        log.exception('health: db 失敗')
    try:
        checks['weight'] = weight.selftest()
    except Exception:
        log.exception('health: weight selftest 失敗')
    ok = all(checks.values())
    return jsonify(ok=ok, **checks), (200 if ok else 503)

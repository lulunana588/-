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

    uid = (ev.get('source') or {}).get('userId')
    token = ev.get('replyToken')

    if not cfg.OWNER_USER_IDS:
        if token and uid:
            line.reply(token, [line.text(
                f'尚未設定使用者。\n你的 userId 是：\n{uid}\n\n'
                '請填入 .env 的 OWNER_USER_IDS 後重啟服務。')])
        return
    if uid not in cfg.OWNER_USER_IDS:
        return  # 非白名單，靜默忽略

    if ev.get('type') == 'follow':
        line.reply(token, [weight.help_msg()], quick=weight.QUICK)
        return

    msg = ev.get('message') or {}
    if ev.get('type') != 'message' or msg.get('type') != 'text':
        return

    msgs = weight.handle(uid, msg.get('text', ''))
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

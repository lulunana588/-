"""VVS小秘書 LINE Messaging API"""
import logging

import requests

import vvs_config as cfg

API = 'https://api.line.me/v2/bot/message/'
log = logging.getLogger('vvs.line')


def text(s):
    return {'type': 'text', 'text': s[:5000]}


def image(url):
    return {'type': 'image', 'originalContentUrl': url, 'previewImageUrl': url}


def _quick(labels):
    return {'items': [{'type': 'action', 'action': {'type': 'message', 'label': l, 'text': l}}
                      for l in labels]}


def _prepare(msgs, quick):
    msgs = [dict(m) for m in msgs[:5]]
    if quick and msgs:
        msgs[-1]['quickReply'] = _quick(quick)
    return msgs


def _post(path, payload):
    try:
        r = requests.post(API + path, json=payload, timeout=10,
                          headers={'Authorization': f'Bearer {cfg.LINE_TOKEN}'})
    except requests.RequestException:
        log.exception('LINE %s 連線失敗', path)
        return False
    if r.status_code != 200:
        log.error('LINE %s %s: %s', path, r.status_code, r.text[:500])
        return False
    return True


def reply(token, msgs, quick=None):
    if not token or not msgs:
        return False
    return _post('reply', {'replyToken': token, 'messages': _prepare(msgs, quick)})


def push(to, msgs, quick=None):
    if not to or not msgs:
        return False
    return _post('push', {'to': to, 'messages': _prepare(msgs, quick)})

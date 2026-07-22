#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
貼圖搬運工 (sticker_bot.py)
------------------------------------------------------------
LINE 貼圖包 → Telegram 貼圖包 轉換機器人

使用方式：
  使用者傳 LINE 貼圖商店網址給機器人，例如：
    https://store.line.me/stickershop/product/34483972/zh-Hant

  機器人會：
    1. 解析貼圖包編號，抓取 LINE 官方 meta 判斷靜態/動態
    2. 下載每張貼圖並轉檔：
         - 靜態貼圖 -> WEBP (長邊縮到 512px)
         - 動態貼圖 -> WEBM (VP9, 512x512, <=3秒, <=256KB)
    3. 以「使用者自己」的身分呼叫 Telegram Bot API 建立貼圖包
       (createNewStickerSet 的 user_id 就是傳訊息的人，
        貼圖包完成後歸屬於使用者本人，不是機器人)
    4. 回傳 t.me/addstickers/... 連結

注意：Telegram 的貼圖包「格式必須單一」(靜態或動態不可混合)。
若 LINE 包同時有靜態+動態貼圖，會分別建立兩個貼圖包並各給一個連結。

部署需求：
  pip install requests Pillow --break-system-packages
  apt install ffmpeg -y   (動態貼圖需要 ffmpeg 轉檔)
"""

import os
import re
import io
import json
import time
import logging
import tempfile
import subprocess
import threading

import requests
from PIL import Image

# ============ 設定 ============
BOT_TOKEN = "8745310156:AAHMnrBNM44FY1h55rrS29EjLg3efMOf-og"
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
BOT_USERNAME = None  # 啟動時自動抓取，不用手動填

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("sticker_bot")

LINE_URL_RE = re.compile(r"stickershop/product/(\d+)")
MAX_STICKERS_PER_SET = 120  # Telegram 上限


# ============ Telegram API 基本呼叫 (含 429 重試) ============
def tg_call(method, data=None, files=None, timeout=120):
    for attempt in range(6):
        try:
            r = requests.post(f"{API}/{method}", data=data, files=files, timeout=timeout)
            j = r.json()
        except Exception as e:
            log.warning(f"{method} 連線失敗，重試中: {e}")
            time.sleep(2)
            continue
        if j.get("ok"):
            return j["result"]
        if j.get("error_code") == 429:
            wait = j.get("parameters", {}).get("retry_after", 3)
            log.warning(f"429 rate limited，等待 {wait}s")
            time.sleep(wait + 1)
            continue
        raise RuntimeError(f"{method} 失敗: {j}")
    raise RuntimeError(f"{method} 重試多次仍失敗")


def send_message(chat_id, text):
    return tg_call("sendMessage", data={"chat_id": chat_id, "text": text})


def get_updates(offset=None, timeout=30):
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(f"{API}/getUpdates", params=params, timeout=timeout + 10)
    j = r.json()
    if not j.get("ok"):
        log.error(f"getUpdates 失敗: {j}")
        return []
    return j["result"]


# ============ LINE 貼圖包抓取 ============
def get_line_product_info(product_id):
    for platform in ("android", "iphone"):
        url = f"https://stickershop.line-scdn.net/stickershop/v1/product/{product_id}/{platform}/productInfo.meta"
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            return r.json()
    raise RuntimeError("找不到這個 LINE 貼圖包，請確認網址是否正確")


def sticker_url(sticker_id, animated):
    base = f"https://stickershop.line-scdn.net/stickershop/v1/sticker/{sticker_id}/iPhone"
    return f"{base}/sticker_animation@2x.png" if animated else f"{base}/sticker@2x.png"


def download_bytes(url):
    r = requests.get(url, timeout=25)
    r.raise_for_status()
    return r.content


def pack_title(meta):
    title = meta.get("title", {})
    return title.get("zh_Hant") or title.get("zh_TW") or title.get("en") or f"LINE貼圖 {meta.get('packageId')}"


# ============ 轉檔 ============
def convert_static_to_webp(png_bytes):
    im = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    w, h = im.size
    scale = 512 / max(w, h)
    im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "WEBP")
    out.seek(0)
    return out.read()


def convert_apng_to_webm(apng_bytes, tmp_dir, idx):
    in_path = os.path.join(tmp_dir, f"in_{idx}.png")
    out_path = os.path.join(tmp_dir, f"out_{idx}.webm")
    with open(in_path, "wb") as f:
        f.write(apng_bytes)

    def run_ffmpeg(bitrate):
        cmd = [
            "ffmpeg", "-y", "-i", in_path,
            "-vf", "scale=512:512:force_original_aspect_ratio=decrease,fps=30,format=yuva420p",
            "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
            "-an", "-t", "3",
            "-b:v", bitrate,
            out_path,
        ]
        subprocess.run(cmd, check=True, capture_output=True)

    run_ffmpeg("256k")
    with open(out_path, "rb") as f:
        data = f.read()
    if len(data) > 256 * 1024:
        run_ffmpeg("120k")
        with open(out_path, "rb") as f:
            data = f.read()
    return data


# ============ 建立 Telegram 貼圖包 ============
def create_sticker_set(user_id, name, title, sticker_items):
    """sticker_items: list of (bytes, format) where format in ('static','video')
       第一批最多包含全部初始貼圖 (createNewStickerSet 支援多張一次建立)"""
    files = {}
    stickers_field = []
    for i, (data, fmt) in enumerate(sticker_items):
        key = f"sticker{i}"
        files[key] = (f"{key}.{'webp' if fmt == 'static' else 'webm'}", data)
        stickers_field.append({
            "sticker": f"attach://{key}",
            "format": fmt,
            "emoji_list": ["🙂"],
        })
    data = {
        "user_id": user_id,
        "name": name,
        "title": title,
        "stickers": json.dumps(stickers_field),
    }
    tg_call("createNewStickerSet", data=data, files=files)


def add_sticker_to_set(user_id, name, data, fmt):
    files = {"sticker0": (f"sticker0.{'webp' if fmt == 'static' else 'webm'}", data)}
    payload = {
        "user_id": user_id,
        "name": name,
        "sticker": json.dumps({
            "sticker": "attach://sticker0",
            "format": fmt,
            "emoji_list": ["🙂"],
        }),
    }
    tg_call("addStickerToSet", data=payload, files=files)


# ============ 主流程：處理一個 LINE 貼圖包 ============
def process_line_pack(chat_id, user_id, product_id):
    send_message(chat_id, "收到！正在讀取 LINE 貼圖包資訊…")
    meta = get_line_product_info(product_id)
    stickers = meta.get("stickers", [])
    if not stickers:
        send_message(chat_id, "這個貼圖包抓不到任何貼圖，請確認網址。")
        return
    if len(stickers) > MAX_STICKERS_PER_SET:
        send_message(chat_id, f"這個包有 {len(stickers)} 張，超過 Telegram 單一貼圖包上限 120 張，只會轉前 120 張。")
        stickers = stickers[:MAX_STICKERS_PER_SET]

    is_animated = bool(meta.get("hasAnimation"))
    title = pack_title(meta)
    kind = "動態(WEBM)" if is_animated else "靜態(WEBP)"
    send_message(chat_id, f"「{title}」共 {len(stickers)} 張，格式：{kind}\n開始下載轉檔，請稍候…")

    fmt = "video" if is_animated else "static"
    converted = []
    tmp_dir = tempfile.mkdtemp(prefix="stk_")
    try:
        for i, s in enumerate(stickers):
            sid = s["id"]
            try:
                raw = download_bytes(sticker_url(sid, is_animated))
                if is_animated:
                    out = convert_apng_to_webm(raw, tmp_dir, i)
                else:
                    out = convert_static_to_webp(raw)
                converted.append(out)
            except Exception as e:
                log.warning(f"貼圖 {sid} 轉檔失敗，略過: {e}")
            if (i + 1) % 10 == 0:
                send_message(chat_id, f"已處理 {i + 1}/{len(stickers)} 張…")

        if not converted:
            send_message(chat_id, "所有貼圖都轉檔失敗，無法建立貼圖包。")
            return

        set_name = f"l{product_id}{int(time.time()) % 10000}_by_{BOT_USERNAME}"
        first_batch = [(d, fmt) for d in converted[:50]]  # 首批最多50張，避免單次請求過大
        send_message(chat_id, "轉檔完成，正在建立你的 Telegram 貼圖包…")
        create_sticker_set(user_id, set_name, title[:64], first_batch)

        for d in converted[50:]:
            add_sticker_to_set(user_id, set_name, d, fmt)
            time.sleep(0.7)

        send_message(chat_id, f"完成！貼圖包已建立在你的帳號下：\nhttps://t.me/addstickers/{set_name}")
    finally:
        try:
            for f in os.listdir(tmp_dir):
                os.remove(os.path.join(tmp_dir, f))
            os.rmdir(tmp_dir)
        except Exception:
            pass


# ============ 訊息處理 ============
def handle_message(msg):
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    text = msg.get("text", "")

    if text.startswith("/start"):
        send_message(
            chat_id,
            "嗨！我是貼圖搬運工🐶\n"
            "把 LINE 貼圖商店的網址傳給我，例如：\n"
            "https://store.line.me/stickershop/product/34483972/zh-Hant\n\n"
            "我會幫你轉成 Telegram 貼圖包，建立在你自己的帳號名下。\n"
            "⚠️ 建立貼圖包前，請先隨便對我說一句話（讓 Telegram 記得你），這樣才能成功建立。",
        )
        return

    m = LINE_URL_RE.search(text)
    if not m:
        send_message(chat_id, "請傳 LINE 貼圖商店的網址給我喔（包含 stickershop/product/數字）。")
        return

    product_id = m.group(1)
    threading.Thread(target=process_line_pack, args=(chat_id, user_id, product_id), daemon=True).start()


# ============ 主迴圈 ============
def main():
    global BOT_USERNAME
    me = tg_call("getMe")
    BOT_USERNAME = me["username"]
    log.info(f"貼圖搬運工啟動，@{BOT_USERNAME}")

    offset = None
    while True:
        try:
            updates = get_updates(offset)
        except Exception as e:
            log.error(f"getUpdates 例外: {e}")
            time.sleep(3)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message")
            if msg and "text" in msg:
                try:
                    handle_message(msg)
                except Exception as e:
                    log.error(f"處理訊息失敗: {e}")
                    try:
                        send_message(msg["chat"]["id"], f"發生錯誤：{e}")
                    except Exception:
                        pass


if __name__ == "__main__":
    main()

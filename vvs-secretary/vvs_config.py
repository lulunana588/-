"""VVS小秘書 設定：讀取同目錄的 .env（systemd 與 cron 共用）"""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent


def _load_env():
    f = BASE / '.env'
    if not f.exists():
        return
    for line in f.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

LINE_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '')
LINE_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
OWNER_USER_IDS = [s.strip() for s in os.environ.get('OWNER_USER_IDS', '').split(',') if s.strip()]
PUBLIC_BASE_URL = os.environ.get('PUBLIC_BASE_URL', 'https://vivicare.duckdns.org').rstrip('/')
DEFAULT_HEIGHT_CM = float(os.environ.get('DEFAULT_HEIGHT_CM', '180'))
DEFAULT_TARGET_KG = float(os.environ.get('DEFAULT_TARGET_KG', '85'))

DATA_DIR = Path(os.environ.get('VVS_DATA_DIR', BASE / 'data'))
DB_PATH = DATA_DIR / 'secretary.db'
CHART_DIR = DATA_DIR / 'charts'
FONT_PATH = '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'

TZ = ZoneInfo('Asia/Taipei')

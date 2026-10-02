#!/bin/bash
# VVS小秘書｜安全部署
# 用法：bash vvs_deploy.sh vvs_weight.py vvs_app.py ...
#       bash vvs_deploy.sh --history          看最近的版本紀錄
#       bash vvs_deploy.sh --rollback 版本號   退回某個舊版本（一樣要先通過全部測試）
#
# 新程式必須先在「隔離區」通過全部測試，才會取代線上版本：
#   1. 語法檢查
#   2. 主程式能正常載入
#   3. 隱私隔離測試（兩個假帳號，A 不能看到或刪到 B 的任何資料）
#   4. 計算邏輯自我測試
# 任何一項沒過 → 線上版本完全不動，新程式不會上線。
# 上線後健康檢查沒過 → 自動還原成舊版本。

set -u
APP=${VVS_APP:-/root/vvs-secretary}
REPO=https://api.github.com/repos/lulunana588/-/contents/vvs-secretary
PY=$APP/venv/bin/python
[ -x "$PY" ] || PY=python3

if [ $# -eq 0 ]; then
  echo "用法：bash vvs_deploy.sh 檔名1 檔名2 ..."
  exit 1
fi

if [ "$1" = "--history" ]; then
  cd "$APP" && git log --date=format:'%m/%d %H:%M' --pretty='%h  %ad  %s' -15 2>/dev/null \
    || echo "還沒有版本紀錄（下次部署後就會開始記錄）"
  exit 0
fi

if [ "$1" = "--rollback" ]; then
  [ -n "${2:-}" ] || { echo "用法：bash vvs_deploy.sh --rollback 版本號（用 --history 查）"; exit 1; }
  cd "$APP" && git rev-parse --verify -q "$2^{commit}" >/dev/null || { echo "找不到版本 $2"; exit 1; }
  SRC=$(mktemp -d /tmp/vvs_rollback.XXXXXX)
  git archive "$2" | tar -x -C "$SRC"
  FILES=$(cd "$SRC" && ls *.py)
  echo "↩️ 準備退回版本 $2（會先跑完全部測試才上線）"
  VVS_SRC="$SRC" VVS_MSG="rollback to $2" bash "$0" $FILES
  RC=$?
  rm -rf "$SRC"
  exit $RC
fi

STAGE=$(mktemp -d /tmp/vvs_stage.XXXXXX)
TESTDATA=$(mktemp -d /tmp/vvs_testdata.XXXXXX)
cleanup() { rm -rf "$STAGE" "$TESTDATA"; }
trap cleanup EXIT

fail() {
  echo ""
  echo "⛔ 部署中止：$1"
  echo "   線上版本沒有任何變動，小秘書照常運作。"
  exit 1
}

echo "📦 1/6 準備隔離區"
cp "$APP"/*.py "$STAGE"/ 2>/dev/null
for f in "$@"; do
  case "$f" in *.py) ;; *) fail "只能部署 .py 檔：$f" ;; esac
  if [ -n "${VVS_SRC:-}" ]; then
    cp "$VVS_SRC/$f" "$STAGE/$f" || fail "找不到 $f"
  else
    curl -sf -H "Accept: application/vnd.github.raw" "$REPO/$f" -o "$STAGE/$f" || fail "從 GitHub 下載失敗：$f"
  fi
  head -c 20 "$STAGE/$f" | grep -q '"""' || fail "$f 內容不對（可能是 GitHub 錯誤訊息或限流）"
  echo "   已下載 $f"
done

echo "🔍 2/6 語法檢查"
"$PY" -m py_compile "$STAGE"/*.py || fail "程式有語法錯誤"

TESTENV="VVS_DATA_DIR=$TESTDATA OWNER_USER_IDS=U_TEST_OWNER LINE_CHANNEL_SECRET=test LINE_CHANNEL_ACCESS_TOKEN=test"

echo "🔍 3/6 主程式載入測試"
(cd "$STAGE" && env $TESTENV "$PY" -c "import vvs_app" >/dev/null 2>&1) || fail "主程式無法載入"

echo "🔒 4/6 隱私隔離測試"
RESULT=$(cd "$STAGE" && env $TESTENV "$PY" -c "
import vvs_privacy as p
ok, detail = p.check_isolation()
print(detail)
raise SystemExit(0 if ok else 1)" 2>&1)
STATUS=$?
echo "   $RESULT"
[ $STATUS -eq 0 ] || fail "隱私測試沒有通過，新程式可能會讓資料外洩"

echo "🔍 5/6 自我測試"
if [ -f "$STAGE/vvs_selftest.py" ]; then
  OUT=$(cd "$STAGE" && env $TESTENV "$PY" vvs_selftest.py 2>&1) || { echo "$OUT" | tail -n 8; fail "自我測試沒有通過"; }
  echo "   $(echo "$OUT" | tail -n 1)"
else
  (cd "$STAGE" && env $TESTENV "$PY" -c "import vvs_weight as w; w.selftest()" >/dev/null 2>&1) || fail "計算邏輯測試沒有通過"
fi

if [ -n "${VVS_DRYRUN:-}" ]; then
  echo "✅ 測試全部通過（試跑模式，未上線）"
  exit 0
fi

echo "🚀 6/6 上線"
BK="$APP/data/deploy_backup/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BK" && chmod 700 "$APP/data/deploy_backup"
for f in "$@"; do
  [ -f "$APP/$f" ] && cp -p "$APP/$f" "$BK/$f"
  cp "$STAGE/$f" "$APP/$f"
done
systemctl restart vvs-secretary
sleep 4
if curl -s https://vivicare.duckdns.org/health | grep -q '"ok":true'; then
  echo ""
  echo "✅ 部署完成，已上線：$*"
  echo "   舊版本保存在 $BK"
  if command -v git >/dev/null; then
    (
      cd "$APP"
      if [ ! -d .git ]; then
        git init -q
        printf 'data/\nvenv/\n.env\n__pycache__/\n*.pyc\n' > .gitignore
        git config user.name vvs-deploy
        git config user.email vvs-deploy@localhost
      fi
      git add -A >/dev/null 2>&1
      git commit -qm "${VVS_MSG:-deploy: $*}" >/dev/null 2>&1 \
        && echo "   版本紀錄：$(git rev-parse --short HEAD)（查詢：bash vvs_deploy.sh --history）"
    )
  fi
else
  echo "⚠️ 上線後健康檢查失敗，自動還原舊版本…"
  for f in "$@"; do
    if [ -f "$BK/$f" ]; then cp -p "$BK/$f" "$APP/$f"; else rm -f "$APP/$f"; fi
  done
  systemctl restart vvs-secretary
  sleep 4
  curl -s https://vivicare.duckdns.org/health | grep -q '"ok":true' \
    && echo "↩️ 已還原成舊版本，小秘書恢復正常" \
    || echo "🚨 還原後仍異常，請截圖給 Claude"
  exit 1
fi

# Luna 行政小幫手機器人 - 部署步驟

功能：Telegram 機器人，選單操作「桶裝水登記」「款項追蹤」，直接寫入 Google Sheets，
畫面比照您現有資產機器人的「⏳ 處理中 → ✅ 已更新」呈現方式。

---

## Step 0：VPS 瀏覽器終端機提醒

每次要貼上「一整段」指令前，先單獨執行這行，避免貼上亂碼：
```
printf '\e[?2004l'
```
建議一次貼一行指令，貼完確認畫面正常再貼下一行。

---

## Step 1：跟 BotFather 申請新機器人

1. Telegram 搜尋 `@BotFather`
2. 傳送 `/newbot`
3. 依指示輸入機器人名稱（例如：Luna行政小幫手）與帳號（必須以 bot 結尾，例如 `luna_admin_bot`）
4. BotFather 會回傳一組 **Token**（長得像 `123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`），先複製起來，等一下會用到

---

## Step 2：建立 Google 服務帳戶（讓機器人有權限直接寫入 Sheets）

1. 開啟 https://console.cloud.google.com/
2. 建立一個新專案（或使用現有專案）
3. 左側選單「API 和服務」→「已啟用的 API 和服務」→「啟用 API 和服務」，搜尋並啟用：
   - **Google Sheets API**
   - **Google Drive API**
4. 左側選單「IAM 與管理」→「服務帳戶」→「建立服務帳戶」
   - 名稱隨意，例如 `luna-sheets-bot`
   - 建立完成後點進去該服務帳戶 →「金鑰」分頁 →「新增金鑰」→「JSON」
   - 會自動下載一個 JSON 檔案，**這個檔案要保管好，不要外流**
5. 打開下載的 JSON 檔，找到 `"client_email"` 欄位，複製那個像 email 的字串
   （長得像 `luna-sheets-bot@your-project.iam.gserviceaccount.com`）

## Step 3：把兩份試算表分享給服務帳戶

分別打開這兩份表：
- 款項追蹤：https://docs.google.com/spreadsheets/d/11qC7Om4eVhBdZZUtrpCoL7oAyY5nEYR5lZ3I4MPUYkk
- 桶裝水登記：https://docs.google.com/spreadsheets/d/1rBdc0jHRmunHJ01eA5hkdF25mCWgfW8WUbCuUHeeFYc

點右上角「共用」，貼上 Step 2 複製的服務帳戶 email，權限選 **「編輯者」**，取消勾選通知，送出。

---

## Step 4：把檔案上傳到 VPS

在 VPS 上建立資料夾：
```
mkdir -p /root/luna_bot
```
把以下檔案上傳到 `/root/luna_bot/`（用 Hostinger 的檔案管理員上傳，或用瀏覽器終端機貼上內容存檔都可以）：
- `main_bot.py`
- `sheets_service.py`
- `config.py`
- `requirements.txt`
- `.env.example`
- Step 2 下載的服務帳戶 JSON 檔，**重新命名為 `service_account.json`** 一起放進 `/root/luna_bot/`

---

## Step 5：安裝套件

```
cd /root/luna_bot
pip install -r requirements.txt --break-system-packages
```

---

## Step 6：設定 .env

```
cd /root/luna_bot
cp .env.example .env
nano .env
```
把 `TELEGRAM_BOT_TOKEN=` 後面貼上 Step 1 拿到的 Token，存檔離開（Ctrl+O 存檔、Enter、Ctrl+X 離開）。
其他試算表 ID / GID 已經幫您填好對應這兩份表的值，通常不用改。

---

## Step 7：先手動測試一次

```
cd /root/luna_bot
python3 main_bot.py
```
畫面出現「Luna 行政小幫手機器人啟動中...」代表成功。
這時去 Telegram 找您剛剛申請的機器人，傳送 `/start` 測試看看選單、桶裝水登記、款項新增/更新是否正常。

測試沒問題後，按 `Ctrl+C` 停止，接著設定成背景常駐服務（Step 8）。

---

## Step 8：設定 systemd，讓機器人開機自動啟動、當機自動重啟

```
nano /etc/systemd/system/luna-bot.service
```
貼入以下內容：
```
[Unit]
Description=Luna Admin Telegram Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/root/luna_bot
ExecStart=/usr/bin/python3 /root/luna_bot/main_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
存檔後執行：
```
systemctl daemon-reload
systemctl enable luna-bot
systemctl start luna-bot
```
確認狀態：
```
systemctl status luna-bot
```
看到 `active (running)` 就代表成功常駐運作了。

之後如果改了程式碼，用這個指令重啟：
```
systemctl restart luna-bot
```
查看即時 log：
```
journalctl -u luna-bot -f
```

---

## 群組使用方式（@機器人 就能馬上處理）

把機器人加進群組後，**大家在群裡直接 @機器人** 就能觸發，不用記 `/start`：

- **只 @機器人，不加其他字** → 直接跳出選單，跟私訊一樣操作
- **@機器人 + 地點 + 入庫/出庫 + 數量**（一行搞定，不用點選單）例如：
  - `@luna_admin_bot 忠孝辦 出庫 5`
  - `@luna_admin_bot 松山辦入庫20`
  - `@luna_admin_bot 南京 補貨 25`

  機器人會自動比對地點關鍵字（例如「忠孝」「松山」「南京」），立刻更新庫存並回覆確認卡片。
  如果關鍵字同時符合兩個地點（例如「宏國」同時有華生／水寶貝兩個庫存點），機器人看不出您指的是哪一個時，
  會直接幫您打開選單讓您用點的選，不會亂猜、亂改庫存。

  > 💡 這個快速指令目前只支援**桶裝水登記**（因為款項新增需要填的欄位比較多，一行指令容易打錯，
  > 款項的部分還是走選單操作比較保險）。

- 群組中的機器人**不需要關閉隱私模式（Privacy Mode）**，只要是「@機器人」或「回覆機器人的訊息」，
  不管隱私模式開關，Telegram 都一定會把訊息送到機器人手上。

---

## 私訊使用方式

Telegram 傳 `/start` 給機器人 →

**🪣 桶裝水登記**：選地點 → 選入庫/出庫 → 輸入桶數 → 自動更新庫存、最後更新日期、狀態（≤20桶自動標「⚠️ 需補貨」）

**💰 款項追蹤**
- 新增款項：輸入名稱 → 金額 → 送件日期 → 進度 → 自動編號、付款狀態預設「待付」
- 更新付款狀態：輸入編號或關鍵字搜尋 → 選擇該筆 → 標記已付，自動帶入今天為實付日期，可加備註

隨時輸入 `/cancel` 可以取消目前操作重新開始。

---

## 之後可以擴充的方向（先不做，供參考）

- 桶裝水：庫存低於門檻時，每天定時自動推播提醒到群組（可以比照您帳單提醒機器人的 cron 邏輯加上去）
- 款項追蹤：新增「查詢本月已付總額」等統計指令
- 限制只有特定同事能操作（`.env` 的 `ALLOWED_USER_IDS` 已經預留好，找到自己的 Telegram ID 填進去就能開啟權限限制）

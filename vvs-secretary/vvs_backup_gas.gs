/**
 * VVS小秘書｜雲端備份接收端（Google Apps Script）
 *
 * VPS 每天把「已加密」的資料庫備份 POST 到這裡，存進 Drive 資料夾「VVS小秘書備份」。
 * 這裡只會碰到加密檔，沒有金鑰，也看不到任何人的數字。
 *
 * 動作（都要帶正確的通行碼 token）：
 *   upload：存一份 vvs_backup_YYYYMMDD.db.enc，順便清掉 60 天前的舊檔（至少保留最新 7 份）
 *   latest：回傳最新一份（給每月還原演練下載回 VPS 驗證）
 *   list  ：列出目前所有備份檔名
 *
 * 設定：專案設定 → 指令碼屬性 → 新增 TOKEN（跟 VPS .env 的 BACKUP_GAS_TOKEN 一樣）
 */
const FOLDER_NAME = 'VVS小秘書備份';
const KEEP_DAYS = 60;
const KEEP_MIN = 7;
const NAME_RE = /^vvs_backup_(\d{8})\.db\.enc$/;

function doPost(e) {
  try {
    const req = JSON.parse(e.postData.contents);
    const token = PropertiesService.getScriptProperties().getProperty('TOKEN');
    if (!token || req.token !== token) return out({ ok: false, error: 'unauthorized' });
    const action = req.action || 'upload';
    if (action === 'upload') return out(upload(req));
    if (action === 'latest') return out(latest());
    if (action === 'list') return out({ ok: true, files: backups().map(f => f.name) });
    return out({ ok: false, error: 'unknown action' });
  } catch (err) {
    return out({ ok: false, error: String(err).slice(0, 200) });
  }
}

function doGet() {
  return out({ ok: false, error: 'POST only' });
}

/** 回傳 JSON */
function out(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function upload(req) {
  if (!NAME_RE.test(req.filename || '')) return { ok: false, error: 'bad filename' };
  const folder = getFolder();
  const old = folder.getFilesByName(req.filename);
  while (old.hasNext()) old.next().setTrashed(true);       // 同一天重傳：取代
  const bytes = Utilities.base64Decode(req.data);
  const file = folder.createFile(Utilities.newBlob(bytes, 'application/octet-stream', req.filename));
  return { ok: true, name: file.getName(), size: file.getSize(), pruned: prune() };
}

function latest() {
  const list = backups();
  if (!list.length) return { ok: false, error: 'no backups' };
  const f = list[list.length - 1];
  return { ok: true, name: f.name,
           data: Utilities.base64Encode(f.file.getBlob().getBytes()) };
}

function prune() {
  const list = backups();
  const cutoff = new Date(Date.now() - KEEP_DAYS * 86400000);
  let n = 0;
  list.slice(0, Math.max(0, list.length - KEEP_MIN)).forEach(f => {
    if (f.date < cutoff) { f.file.setTrashed(true); n++; }
  });
  return n;
}

/** 依檔名日期由舊到新排序的備份檔 */
function backups() {
  const it = getFolder().getFiles();
  const list = [];
  while (it.hasNext()) {
    const file = it.next();
    const m = NAME_RE.exec(file.getName());
    if (!m) continue;
    const d = m[1];
    list.push({ file: file, name: file.getName(),
                date: new Date(+d.slice(0, 4), +d.slice(4, 6) - 1, +d.slice(6, 8)) });
  }
  return list.sort((a, b) => a.name < b.name ? -1 : 1);
}

function getFolder() {
  const props = PropertiesService.getScriptProperties();
  const id = props.getProperty('FOLDER_ID');
  if (id) {
    try { const f = DriveApp.getFolderById(id); if (!f.isTrashed()) return f; } catch (e) {}
  }
  const it = DriveApp.getFoldersByName(FOLDER_NAME);
  const folder = it.hasNext() ? it.next() : DriveApp.createFolder(FOLDER_NAME);
  props.setProperty('FOLDER_ID', folder.getId());
  return folder;
}

/** 在編輯器裡手動執行一次，用來授權 Drive 權限並建立資料夾 */
function setup() {
  const folder = getFolder();
  Logger.log('備份資料夾：' + folder.getName() + '｜目前備份 ' + backups().length + ' 份');
  Logger.log(PropertiesService.getScriptProperties().getProperty('TOKEN') ? '通行碼 TOKEN 已設定 ✅' : '⚠️ 還沒設定通行碼 TOKEN');
}

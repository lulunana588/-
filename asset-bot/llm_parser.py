# -*- coding: utf-8 -*-
"""
用 Groq LLM 解析整段貼上的資產異動清單文字(如入庫/領用/故障/換座位/變更保管人/遺失),
只負責「抽取結構化資訊」,實際欄位怎麼改、備註怎麼寫,由 asset_bot.py 依商業規則決定,
不讓 LLM 直接決定最終要寫入試算表的值,降低寫錯正式資料的風險。
"""
import json
import requests
from config import GROQ_API_KEY, GROQ_MODEL, BATCH_ACTION_TYPES

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

SYSTEM_PROMPT = f"""你是資產異動清單的文字解析器。使用者會貼上一段包含多筆資產異動的文字,
可能包含以下幾種標題(前面可能有📍符號):{'、'.join(BATCH_ACTION_TYPES)}。

請把文字拆解成一個 JSON 物件,格式為:
{{"actions": [ ... ]}}

actions 陣列中每一筆資產(每個編號)都是一個獨立物件,依類型輸出以下欄位(沒有的欄位就不要輸出該key):

- type: "入庫" | "領用" | "故障" | "換座位" | "變更保管人" | "遺失" (必填)
- asset_id: 資產編號,例如 "B-01-023"(必填,去除頭尾空白)
- person: 該段落標示的花名/人名(入庫=原使用人、領用=領用人、故障/遺失=回報人,若有的話)
- reason: 故障原因或遺失原因(故障、遺失類型才有)
- old_location: 換座位類型的舊座位/舊區域
- new_location: 換座位類型的新座位/新區域;或變更保管人類型使用者額外提供的新所在區域;或領用類型括號內的所在區域
- new_keeper: 變更保管人類型的新保管人姓名
- old_keeper: 變更保管人類型括號內的原保管人姓名(若有)
- department: 使用部門(領用類型若文字中有提到部門就填,沒提到就不要輸出這個key)
- emp_id: 員編(領用類型若文字中有提到員編就填,沒提到就不要輸出這個key)

規則:
1. 一個標題底下如果列了多個編號(例如手機*4後面接4個編號,或換座位列出5個不同品項編號),
   要展開成多筆各自獨立的 action,共用同一個 person/location 等資訊。
2. 只輸出你能從文字中清楚判讀出來的欄位,不確定的欄位不要編造,直接省略。
3. 資產編號請保留原始格式(字母、數字、連字號)。
4. 只回傳 JSON,不要有任何其他文字、不要用 markdown code fence。
"""


def parse_batch_text(text: str):
    """
    呼叫 Groq API 解析文字,回傳 actions list(每個是 dict)。
    解析失敗會拋出例外,呼叫端需自行 try/except。
    """
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    resp = requests.post(GROQ_URL, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    actions = parsed.get("actions", [])

    # 基本驗證,過濾掉缺少必要欄位或類型不合法的項目
    valid_actions = []
    for a in actions:
        if not a.get("type") or not a.get("asset_id"):
            continue
        if a["type"] not in BATCH_ACTION_TYPES:
            continue
        a["asset_id"] = str(a["asset_id"]).strip()
        valid_actions.append(a)
    return valid_actions

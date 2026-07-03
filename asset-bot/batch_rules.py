# -*- coding: utf-8 -*-
"""
把 llm_parser 解析出的結構化 action,依照 Luna 確認過的規則,
轉成實際要寫入試算表的欄位(fields)與備註(note)。
所有數值判斷都在這裡用 Python 明確規則決定,不交給 LLM 自由發揮。
"""


def build_plan(action: dict):
    """
    回傳 (ok, fields, note, error_msg)
    ok=False 代表缺少必要資訊,無法安全套用,呼叫端應標記為「需人工確認」而不是自動寫入。
    """
    a_type = action.get("type")
    person = (action.get("person") or "").strip()
    reason = (action.get("reason") or "").strip()
    old_location = (action.get("old_location") or "").strip()
    new_location = (action.get("new_location") or "").strip()
    new_keeper = (action.get("new_keeper") or "").strip()
    old_keeper = (action.get("old_keeper") or "").strip()
    department = (action.get("department") or "").strip()
    emp_id = (action.get("emp_id") or "").strip()

    if a_type == "入庫":
        fields = {"status": "庫存", "location": "倉庫", "keeper": "倉庫"}
        note = f"入庫(原使用人:{person})" if person else "入庫"
        return True, fields, note, None

    if a_type == "領用":
        if not person or not new_location:
            return False, {}, "", "缺少領用人花名或所在區域"
        fields = {"status": "使用中", "keeper": person, "location": new_location}
        if department:
            fields["department"] = department
        if emp_id:
            fields["emp_id"] = emp_id
        note = f"領用(領用人:{person})"
        return True, fields, note, None

    if a_type == "故障":
        if not reason:
            return False, {}, "", "缺少故障原因"
        note = f"故障:{reason}" + (f"(回報人:{person})" if person else "")
        return True, {}, note, None

    if a_type == "換座位":
        if not new_location:
            return False, {}, "", "缺少新座位/新區域"
        fields = {"location": new_location}
        if old_location:
            note = f"換座位:{old_location}→{new_location}"
        else:
            note = f"換座位:改到{new_location}"
        if person:
            note += f"(花名:{person})"
        return True, fields, note, None

    if a_type == "變更保管人":
        if not new_keeper or not new_location:
            return False, {}, "", "缺少新保管人或新所在區域"
        fields = {"keeper": new_keeper, "location": new_location}
        note = f"變更保管人:{old_keeper or '原保管人'}→{new_keeper}"
        return True, fields, note, None

    if a_type == "遺失":
        if not reason:
            return False, {}, "", "缺少遺失原因"
        note = f"遺失:{reason}" + (f"(回報人:{person})" if person else "")
        return True, {}, note, None

    return False, {}, "", f"未知類型:{a_type}"

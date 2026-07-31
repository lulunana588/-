# -*- coding: utf-8 -*-
"""
把門號異動的結構化 action(type/person/old_person/reason)轉成實際要寫入的欄位跟附註文字。
"""


def build_plan(action: dict):
    """
    回傳 (ok, fields, note, error_msg)
    fields 的 key 只會是 "name" / "type",呼叫端會依實際欄位字母寫入。
    """
    a_type = action.get("type")
    person = (action.get("person") or "").strip()
    old_person = (action.get("old_person") or "").strip()
    reason = (action.get("reason") or "").strip()

    if a_type == "入庫":
        fields = {"name": "庫存", "type": "庫存"}
        note = f"入庫(原使用人:{person})" if person else "入庫"
        return True, fields, note, None

    if a_type == "領用":
        if not person:
            return False, {}, "", "缺少領用人花名"
        fields = {"name": person, "type": "公務機"}
        note = f"領用(領用人:{person})"
        return True, fields, note, None

    if a_type == "轉移":
        if not person:
            return False, {}, "", "缺少新使用人花名"
        fields = {"name": person}
        if old_person:
            note = f"轉移:{old_person}→{person}"
        else:
            note = f"轉移(改為:{person})"
        return True, fields, note, None

    if a_type == "死號":
        fields = {"type": "死號"}
        note = f"死號:{reason}" if reason else "死號"
        return True, fields, note, None

    if a_type == "遺失":
        fields = {"type": "死號"}
        note = f"遺失:{reason}" if reason else "遺失"
        return True, fields, note, None

    return False, {}, "", f"未知類型:{a_type}"

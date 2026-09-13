"""Rewards Dashboard 页面数据解析（RSC JSON 抠取 + 进度正则）"""
import json
import re
from datetime import datetime

from utils.config import DEFAULT_MOBILE_SEARCHES, DEFAULT_PC_SEARCHES


def _extract_json_array(html: str, key: str) -> str | None:
    """从 HTML 中用括号计数提取 JSON 数组"""
    idx = html.find(key)
    if idx < 0:
        return None
    # 跳过 key 和冒号
    start = html.find("[", idx)
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(html)):
        ch = html[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return html[start:i + 1]
    return None


def _extract_json_object(html: str, key: str) -> str | None:
    """从 HTML 中用括号计数提取 JSON 对象"""
    idx = html.find(key)
    if idx < 0:
        return None
    start = html.find("{", idx)
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(html)):
        ch = html[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return html[start:i + 1]
    return None


def parse_dashboard_data(page) -> dict:
    """从页面提取任务数据"""
    result = {
        "claimable_points": 0,
        "claim_entries": [],
        "daily_set_items": [],
        "pc_search_total": DEFAULT_PC_SEARCHES,
        "pc_search_done": 0,
        "mobile_search_total": DEFAULT_MOBILE_SEARCHES,
        "mobile_search_done": 0,
    }

    html = page.content()

    # 提取 dailySetItems
    arr_text = _extract_json_array(html, "dailySetItems")
    if arr_text:
        try:
            items = json.loads(arr_text)
        except json.JSONDecodeError:
            try:
                fixed = arr_text.replace('\\"', '"').replace("\\\\", "\\")
                items = json.loads(fixed)
            except json.JSONDecodeError:
                items = []

        today_str = datetime.now().strftime("%m/%d/%Y")
        for item in items:
            item_date = item.get("date", "")
            if not item_date or item.get("isCompleted") or item_date != today_str:
                continue
            result["daily_set_items"].append({
                "title": item.get("title", ""),
                "points": item.get("points", 0),
                "destination": item.get("destination", ""),
                "offerId": item.get("offerId", ""),
                "date": item_date,
            })

        if not result["daily_set_items"]:
            # 回退：用最新日期
            dates = [item.get("date", "") for item in items if item.get("date") and not item.get("isCompleted")]
            if dates:
                latest = sorted(dates)[-1]
                for item in items:
                    if item.get("date") == latest and not item.get("isCompleted"):
                        result["daily_set_items"].append({
                            "title": item.get("title", ""),
                            "points": item.get("points", 0),
                            "destination": item.get("destination", ""),
                            "offerId": item.get("offerId", ""),
                            "date": latest,
                        })

    # 提取 pointClaim
    obj_text = _extract_json_object(html, "pointClaim")
    if obj_text:
        try:
            claim_data = json.loads(obj_text)
            result["claimable_points"] = claim_data.get("points", 0)
            result["claim_entries"] = claim_data.get("entries", [])
        except json.JSONDecodeError:
            try:
                fixed = obj_text.replace('\\"', '"').replace("\\\\", "\\")
                claim_data = json.loads(fixed)
                result["claimable_points"] = claim_data.get("points", 0)
            except json.JSONDecodeError:
                pass

    # 从页面文本提取进度
    body_text = page.evaluate("() => document.body.innerText || ''")

    for pattern in [
        r'必应搜索[:\s]*(\d+)\s*/\s*(\d+)',
        r'搜索[:\s]*(\d+)\s*/\s*(\d+)',
    ]:
        m = re.search(pattern, body_text)
        if m:
            result["pc_search_done"] = int(m.group(1))
            result["pc_search_total"] = int(m.group(2))
            break

    mobile_match = re.search(r'移动搜索[:\s]*(\d+)\s*/\s*(\d+)', body_text)
    if mobile_match:
        result["mobile_search_done"] = int(mobile_match.group(1))
        result["mobile_search_total"] = int(mobile_match.group(2))

    daily_match = re.search(r'活动[:\s]*(\d+)\s*/\s*(\d+)', body_text)
    if daily_match:
        result["daily_activities_done"] = int(daily_match.group(1))
        result["daily_activities_total"] = int(daily_match.group(2))

    return result

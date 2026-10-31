"""评测集加载与 L1 断言原语(测试层与报告脚本共用)。"""
from __future__ import annotations

from pathlib import Path

import yaml

EVAL_DIR = Path(__file__).parent / "eval_sets"


def load_eval_cases() -> list[dict]:
    """加载 eval_sets/ 下全部用例,并做质量门禁(id 查重/必填字段)——评测集本身也是被测资产。"""
    cases: list[dict] = []
    for path in sorted(EVAL_DIR.glob("*.yaml")):
        for case in yaml.safe_load(path.read_text(encoding="utf-8")) or []:
            for field in ("id", "input", "tags"):
                assert case.get(field), f"{path.name} 存在缺少 {field} 的用例: {case}"
            case["_file"] = path.name
            cases.append(case)
    ids = [c["id"] for c in cases]
    dups = {i for i in ids if ids.count(i) > 1}
    assert not dups, f"评测集 id 重复(违反 SCHEMA 约定): {dups}"
    return cases


def norm_value(value):
    """数值容错: '22' 与 22 视为相等(模型输出类型不稳定,归一化后比对)。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value.strip()
    return value


def match_intent(got_entry: dict, expected: dict) -> bool:
    """intent 精确匹配 + slots 子集匹配(只查期望里写了的槽位)。"""
    if got_entry.get("intent") != expected["intent"]:
        return False
    exp_slots = expected.get("slots") or {}
    got_slots = got_entry.get("slots") or {}
    return all(
        key in got_slots and norm_value(got_slots[key]) == norm_value(val)
        for key, val in exp_slots.items()
    )

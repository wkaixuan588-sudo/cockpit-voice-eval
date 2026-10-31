"""L1 规则断言层: 逐条跑评测集,断言意图/槽位/播报关键词。

确定性断言(零 judge 成本),断言语义见 eval_sets/SCHEMA.md:
intent 精确匹配 / slots 子集匹配 / 数值容错 / reply_contains 子串。
"""
import os

import pytest

from evalsets import load_eval_cases, match_intent

pytestmark = [
    pytest.mark.rule,
    pytest.mark.skipif(
        os.getenv("SUT_MODE", "real") == "mock",
        reason="mock 桩仅调流水线,不做语义断言",
    ),
]


def _case_params():
    """支持 xfail 字段:已知缺陷/已知平台限制的用例标记为 xfail(原因写入用例),持续跟踪。"""
    for case in load_eval_cases():
        if case.get("xfail"):
            yield pytest.param(case, marks=pytest.mark.xfail(reason=case["xfail"], strict=False), id=case["id"])
        else:
            yield pytest.param(case, id=case["id"])


@pytest.mark.parametrize("case", list(_case_params()))
def test_eval_case(case, sut):
    resp = sut.ask(case["input"])
    assert resp.parsed is not None, f"输出不可解析为 JSON: {resp.raw[:150]}"

    got = resp.parsed.get("intents")
    assert isinstance(got, list) and got, f"缺少非空 intents 数组: {resp.parsed}"

    expect = case.get("expect") or {}
    for exp in expect.get("intents", []):
        assert any(match_intent(g, exp) for g in got), (
            f"未命中期望意图 {exp['intent']} (slots={exp.get('slots')}), 实际: {got}"
        )

    reply = resp.parsed.get("spoken_reply", "")
    for keyword in expect.get("reply_contains", []):
        assert keyword in reply, f"播报未包含 '{keyword}': {reply}"

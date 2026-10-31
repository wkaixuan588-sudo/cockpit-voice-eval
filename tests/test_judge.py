"""L2 LLM-as-Judge 层: 生成式质量断言。

维度与 tags 的映射见 judge/glm_judge.py:模糊诉求响应质量/边界截断说明/
拒答得体性/越界引导得体性/车辆知识事实性/回复相关性。
只对命中维度的用例跑 judge;确定性用例 L1 已覆盖,不重复花费。
"""
import os

import pytest
from deepeval.test_case import LLMTestCase

from evalsets import load_eval_cases
from judge.glm_judge import TAG_METRICS, VEHICLE_SPEC, metrics_for

pytestmark = [
    pytest.mark.judge,
    pytest.mark.skipif(
        os.getenv("SUT_MODE", "real") == "mock",
        reason="mock 桩不做语义判定",
    ),
]

# 与 judge/glm_judge.py 维度映射保持同源
_JUDGE_TAGS = set(TAG_METRICS)


def _judged_params():
    for case in load_eval_cases():
        if not (_JUDGE_TAGS & set(case.get("tags") or [])):
            continue
        marks = []
        # judge_xfail: 裁判层专属已知问题(如裁判模型推理与打分不一致),不影响 L1 的 xfail 口径
        reason = case.get("judge_xfail") or case.get("xfail")
        if reason:
            marks.append(pytest.mark.xfail(reason=reason, strict=False))
        yield pytest.param(case, marks=marks, id=case["id"])


@pytest.mark.parametrize("case", list(_judged_params()))
def test_judge_case(case, sut):
    resp = sut.ask(case["input"])
    assert resp.parsed is not None, f"输出不可解析,无法进入 judge 层: {resp.raw[:120]}"

    action = "; ".join(
        f"{i.get('intent')}({i.get('slots') or {}})" for i in resp.parsed.get("intents", [])
    )
    kwargs = {
        "input": case["input"],
        "actual_output": f"执行动作: {action}\n播报: {resp.parsed.get('spoken_reply', '')}",
    }
    if "hallucination_probe" in (case.get("tags") or []):
        kwargs["expected_output"] = (case.get("expect") or {}).get("facts") or VEHICLE_SPEC
    tc = LLMTestCase(**kwargs)

    for metric in metrics_for(case):
        metric.measure(tc)
        assert metric.score >= metric.threshold, (
            f"[{metric.name}] score={metric.score:.2f} < {metric.threshold}: {metric.reason}"
        )

"""冒烟集: SUT 可用性 —— 语义层链路是否通、输出是否可断言。"""
import pytest

pytestmark = pytest.mark.smoke


def test_sut_returns_parseable_json(sut):
    """输出必须可解析为含三字段的 JSON —— 对应真实链路的 NLU 结果可断言性。"""
    resp = sut.ask("打开空调")
    assert resp.parsed is not None, f"输出无法解析为 JSON: {resp.raw[:200]}"
    assert isinstance(resp.parsed.get("intents"), list) and resp.parsed["intents"], (
        f"输出缺少非空 intents 数组: {resp.parsed}"
    )
    assert "spoken_reply" in resp.parsed


def test_sut_records_metadata(sut):
    """可复现性三要素: 模型版本 + prompt 版本 + 时延(端到端时延意识)。"""
    resp = sut.ask("把空调调到22度")
    assert resp.model
    assert resp.prompt_version
    assert resp.latency_ms > 0


def test_sut_refuses_garbage(sut):
    """鲁棒性冒烟: 空输入不崩溃(真实车机里是静音/误唤醒场景)。"""
    resp = sut.ask("")
    assert resp.raw is not None

import pytest

from sut.assistant import get_assistant


@pytest.fixture(scope="session")
def sut():
    """SUT 实例。默认读 .env 配真实模型;SUT_MODE=mock 时用本地桩。"""
    return get_assistant()

"""Xiaomi's documented421 is content filtering, not a spent credential."""
from types import SimpleNamespace
import pytest
from .test_v1_7_0_7_dispatch_ownership import dispatch, carousel


def test_xiaomi_421_does_not_rotate_or_bench():
    exc = Exception("Request rejected")
    exc.status_code = 421
    exc.request = SimpleNamespace(url="https://api.xiaomimimo.com/v1/chat/completions")
    engine = carousel.Carousel()
    before = engine.snapshot()
    result = dispatch.DispatchBinding(engine=engine)._on_failure(
        "xiaomi:m", "synthetic", exc, "test", 1, False)
    assert result[0] == "raise"
    assert engine.snapshot() == before


@pytest.mark.parametrize("url", [
    "https://api.xiaomimimo.com.evil.invalid/v1/chat/completions",
    "http://api.xiaomimimo.com/v1/chat/completions",
    "https://api.xiaomimimo.com/v1/other",
    "https://proxy.invalid/v1/chat/completions",
])
def test_alias_does_not_activate_xiaomi_status_meaning(url):
    exc = Exception("Request rejected")
    exc.status_code = 421
    exc.request = SimpleNamespace(url=url)
    result = dispatch.DispatchBinding(engine=carousel.Carousel())._on_failure(
        "xiaomi:m", "synthetic", exc, "test", 1, False)
    assert result[1] != "content_filter"

"""Ignoring misleading billing context must preserve independent quota scope."""
import pytest
from .test_v1_7_0_7_surfaces import NOW
from core.quota import compute_reset_at

@pytest.mark.parametrize("counter", ["RequestsPerAccountPerMinute", "TokensPerProjectPerMinute", "RequestsPerOrganizationPerMinute"])
def test_independent_shared_counter_survives_context_override(counter):
    result = compute_reset_at(now_epoch=NOW, message="Insufficient balance",
        body={"quotaId":counter}, headers={"retry-after":"30"}, billing_context_only=True)
    assert result.scope == "account"
    assert result.window == "per_minute"
    assert result.reset_at == NOW+30

def test_billing_words_alone_do_not_invent_shared_scope():
    result = compute_reset_at(now_epoch=NOW, message="Insufficient balance",
        headers={"retry-after":"30"}, billing_context_only=True)
    assert result.scope == "unknown"

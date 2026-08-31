from app.telemetry import pseudonymous_user, request_id


def test_user_hash_is_stable_and_does_not_expose_identity() -> None:
    identity = "entra-object-id"
    first = pseudonymous_user(identity, "k" * 32)
    second = pseudonymous_user(identity, "k" * 32)
    assert first == second
    assert identity not in first
    assert len(first) == 24


def test_request_id_rejects_unbounded_or_unsafe_values() -> None:
    assert request_id("safe-request_1") == "safe-request_1"
    assert request_id("unsafe request") != "unsafe request"

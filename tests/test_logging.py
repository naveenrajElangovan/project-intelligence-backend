import logging

from app.logging import _SanitizeAccessLogFilter


def test_access_log_filter_redacts_oauth_code_and_state() -> None:
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=(
            "127.0.0.1:1234",
            "GET",
            "/v1/integrations/atlassian/callback?state=secret-state&code=secret-code&safe=value",
            "1.1",
            200,
        ),
        exc_info=None,
    )

    assert _SanitizeAccessLogFilter().filter(record) is True
    assert "secret-state" not in str(record.args)
    assert "secret-code" not in str(record.args)
    assert "safe=value" in str(record.args)

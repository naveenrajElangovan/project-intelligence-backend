"""A session that works and then keeps dropping is almost never an expired token:
the provider renews in-process. It is pool_recycle rebuilding connections while
the firewall rule still names an old public IP."""

from app.infrastructure.sql.database import (
    FIREWALL_REMEDY,
    IDENTITY_REMEDY,
    PAUSED_REMEDY,
    connection_refusal_remedy,
)


def test_a_firewall_rejection_points_at_the_sync_script():
    detail = (
        "('42000', \"[42000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]"
        "Cannot open server 'pi-sql' requested by the login. Client with IP address "
        "'203.0.113.9' is not allowed to access the server. (40615)\")"
    )
    assert connection_refusal_remedy(detail) == FIREWALL_REMEDY
    assert "sync_sql_access.sh" in FIREWALL_REMEDY


def test_the_numeric_code_alone_is_enough():
    assert connection_refusal_remedy("driver said 40615 and nothing else") == FIREWALL_REMEDY


def test_a_login_rejection_points_at_the_identity_check():
    assert connection_refusal_remedy("Login failed for user '<token-identified>'") == (
        IDENTITY_REMEDY
    )
    assert "check_runtime_identity.py" in IDENTITY_REMEDY


def test_an_unrecognised_error_is_not_mislabelled():
    # Guessing here would send the reader to the wrong remedy, which is worse
    # than leaving the driver error to speak for itself.
    assert connection_refusal_remedy("TCP provider: timeout expired") is None


def test_an_empty_detail_is_not_a_diagnosis():
    assert connection_refusal_remedy("") is None


def test_a_paused_free_tier_database_is_named_as_such():
    detail = (
        "('HY000', '[HY000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]"
        "This database has reached the monthly free amount allowance for the month "
        "of August 2026 and is paused for the remainder of the month. (42119) "
        "(SQLDriverConnect)')"
    )
    assert connection_refusal_remedy(detail) == PAUSED_REMEDY


def test_a_paused_database_is_not_reported_as_a_firewall_problem():
    # It is a refused connection like any other, so ordering matters: read this
    # as a firewall issue and the reader runs a script that cannot help.
    detail = "42119 and also is not allowed to access the server"
    assert connection_refusal_remedy(detail) == PAUSED_REMEDY

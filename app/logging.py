import logging
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_HANDLER_NAME = "project-intelligence-console"
_TELEMETRY_HANDLER_NAME = "project-intelligence-telemetry-json"
_SENSITIVE_QUERY_PARAMETERS = {"code", "state"}


class _SanitizeAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not isinstance(record.args, tuple) or len(record.args) < 3:
            return True
        request_target = record.args[2]
        if not isinstance(request_target, str) or "?" not in request_target:
            return True
        parsed = urlsplit(request_target)
        sanitized_query = urlencode(
            [
                (name, "[REDACTED]" if name.lower() in _SENSITIVE_QUERY_PARAMETERS else value)
                for name, value in parse_qsl(parsed.query, keep_blank_values=True)
            ]
        )
        sanitized_target = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, sanitized_query, parsed.fragment)
        )
        arguments = list(record.args)
        arguments[2] = sanitized_target
        record.args = tuple(arguments)
        return True


def configure_application_logging(level: str) -> None:
    """Configure logs emitted by application modules without altering Uvicorn logs."""
    application_logger = logging.getLogger("app")
    application_logger.setLevel(_resolve_level(level))

    if not any(handler.get_name() == _HANDLER_NAME for handler in application_logger.handlers):
        handler = logging.StreamHandler()
        handler.set_name(_HANDLER_NAME)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )
        application_logger.addHandler(handler)

    application_logger.propagate = False

    telemetry_logger = logging.getLogger("app.telemetry")
    telemetry_logger.setLevel(_resolve_level(level))
    if not any(
        handler.get_name() == _TELEMETRY_HANDLER_NAME
        for handler in telemetry_logger.handlers
    ):
        telemetry_handler = logging.StreamHandler()
        telemetry_handler.set_name(_TELEMETRY_HANDLER_NAME)
        telemetry_handler.setFormatter(logging.Formatter("%(message)s"))
        telemetry_logger.addHandler(telemetry_handler)
    telemetry_logger.propagate = False

    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, _SanitizeAccessLogFilter) for item in access_logger.filters):
        access_logger.addFilter(_SanitizeAccessLogFilter())


def _resolve_level(value: str) -> int:
    resolved = logging.getLevelName(value.strip().upper())
    return resolved if isinstance(resolved, int) else logging.INFO

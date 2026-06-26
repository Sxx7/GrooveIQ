"""GrooveIQ – Logging configuration."""

import json
import logging
import sys

# ---------------------------------------------------------------------------
# Sensitive field redaction
# ---------------------------------------------------------------------------

_SENSITIVE_SUBSTRINGS = ("api_key", "password", "secret", "token", "session_key", "authorization", "credential")


def _redact_sensitive_args(args: tuple) -> tuple:
    """Redact any string arg that looks like a secret (heuristic)."""
    out = []
    for arg in args:
        if isinstance(arg, str) and len(arg) >= 32 and not arg.startswith(("/", "http")):
            # Looks like a raw key/token — redact all but the first 4 chars
            out.append(arg[:4] + "***REDACTED***")
        else:
            out.append(arg)
    return tuple(out)


class _JsonFormatter(logging.Formatter):
    """JSON log formatter that properly escapes all fields.

    Unlike a plain format string with %(message)s, this uses json.dumps()
    so user-controlled data (user_id, artist names, error messages) cannot
    break the JSON structure or inject false log entries.
    """

    def format(self, record: logging.LogRecord) -> str:
        # Redact sensitive fields in the log message args
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: "***REDACTED***" if any(s in k.lower() for s in _SENSITIVE_SUBSTRINGS) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = _redact_sensitive_args(record.args)

        message = record.getMessage()

        entry = {
            "time": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": message,
        }
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def setup_logging():
    from app.core.config import settings

    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    if settings.LOG_JSON:
        try:
            import structlog

            structlog.configure(
                wrapper_class=structlog.make_filtering_bound_logger(level),
                logger_factory=structlog.PrintLoggerFactory(sys.stdout),
            )
        except ImportError:
            pass
        # Always configure the stdlib root logger so that modules using
        # logging.getLogger(__name__) actually produce output.
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_JsonFormatter())
        logging.basicConfig(level=level, handlers=[handler])
    else:
        logging.basicConfig(level=level, stream=sys.stdout, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # Quiet httpx's INFO-level request logging. It logs the full request URL —
    # query string included — on every call, which leaks secrets such as the
    # Last.fm api_key (e.g. ws.audioscrobbler.com/2.0/?...&api_key=...) into the
    # app log. These logs are shipped off-box, so the credential would land in
    # the aggregator in plaintext. WARNING still surfaces genuine httpx problems
    # while dropping the noisy, secret-bearing per-request lines; outbound
    # failures are already logged by the callers (see app/services/lastfm_*.py).
    logging.getLogger("httpx").setLevel(logging.WARNING)

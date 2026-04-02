"""GrooveIQ – Logging configuration."""
import logging, sys

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
            logging.basicConfig(level=level, stream=sys.stdout,
                format='{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}')
    else:
        logging.basicConfig(level=level, stream=sys.stdout,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

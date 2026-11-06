"""One logging pipeline for entry points (DECISIONS D-034).

The code base mixes loguru and the standard library. Unconfigured, loguru writes every DEBUG
line to stderr while standard-library INFO lines are dropped. configure_logging() routes the
standard library into loguru and applies one level: ALETHEIA_LOG_LEVEL, default INFO, for the
application's own modules; third-party libraries stay at WARNING (they log request URLs).
Entry points call it; importing modules never configures logging.
"""

import logging
import os
import sys

from loguru import logger

LEVELS = ('DEBUG', 'INFO', 'WARNING', 'ERROR')
FORMAT = '{time:HH:mm:ss} | {level: <7} | {name} | {message}'


class _ToLoguru(logging.Handler):
    """Forward standard-library records to loguru, keeping the level and logger name."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        logger.patch(lambda r: r.update(name=record.name)).opt(exception=record.exc_info).log(
            level, record.getMessage())


def _application_record(record) -> bool:
    return record['name'].startswith('app.')


def configure_logging(level: str | None = None) -> str:
    """Install the single stderr sink and return the level in use."""
    level = (level or os.environ.get('ALETHEIA_LOG_LEVEL') or 'INFO').upper()
    if level not in LEVELS:
        level = 'INFO'
    threshold = logger.level(level).no
    warning = logger.level('WARNING').no
    logger.remove()
    logger.add(sys.stderr, format=FORMAT, colorize=False,
               filter=lambda r: r['level'].no >= (threshold if _application_record(r) else max(threshold, warning)))
    logging.basicConfig(handlers=[_ToLoguru()], level=logging.WARNING, force=True)
    logging.getLogger('app').setLevel(level)
    return level

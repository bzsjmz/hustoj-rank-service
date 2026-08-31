from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("oj_rank")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(formatter)

    logfile = RotatingFileHandler(
        log_dir / "crawler.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    logfile.setFormatter(formatter)
    logger.addHandler(stdout)
    logger.addHandler(logfile)
    return logger

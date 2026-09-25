"""Integrated logging: ring buffer + optional file + listeners."""
from __future__ import annotations

import logging
import logging.handlers
import os
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, List, Optional

LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING,
          "error": logging.ERROR, "critical": logging.CRITICAL}


class RingHandler(logging.Handler):
    def __init__(self, capacity: int = 5000):
        super().__init__()
        self.records: Deque[dict] = deque(maxlen=capacity)
        self.listeners: List[Callable[[dict], None]] = []
        self.seq = 0
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord):
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        with self._lock:
            self.seq += 1
            rec = {"seq": self.seq, "time": record.created, "level": record.levelname.lower(),
                   "name": record.name, "message": msg}
            self.records.append(rec)
        for l in list(self.listeners):
            try:
                l(rec)
            except Exception:
                pass

    def tail(self, n: int = 100, level: Optional[str] = None, pattern: Optional[str] = None) -> List[dict]:
        with self._lock:
            recs = list(self.records)
        if level:
            lv = LEVELS.get(level, 0)
            recs = [r for r in recs if LEVELS.get(r["level"], 0) >= lv]
        if pattern:
            p = pattern.lower()
            recs = [r for r in recs if p in r["message"].lower() or p in r["name"].lower()]
        return recs[-n:]


def format_record(r: dict, color: bool = True) -> str:
    t = time.strftime("%H:%M:%S", time.localtime(r["time"]))
    lv = r["level"]
    if not color:
        return "%s %-7s %s: %s" % (t, lv.upper(), r["name"], r["message"])
    col = {"debug": "90", "info": "36", "warning": "33", "error": "31", "critical": "1;31"}.get(lv, "0")
    return "\x1b[90m%s\x1b[0m \x1b[%sm%-7s\x1b[0m \x1b[90m%s:\x1b[0m %s" % (t, col, lv.upper(), r["name"], r["message"])


def setup_logging(name: str = "pytermwm", level: str = "info", file: Optional[str] = None,
                  capacity: int = 5000, stderr: bool = False) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(LEVELS.get(level, logging.INFO))
    logger.propagate = False
    ring = None
    for h in list(logger.handlers):
        if isinstance(h, RingHandler):
            ring = h
        elif isinstance(h, (logging.FileHandler, logging.StreamHandler)):
            logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
    if ring is None:
        ring = RingHandler(capacity)
        logger.addHandler(ring)
    if file:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(file)), exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(file, maxBytes=1 << 20, backupCount=3)
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
            logger.addHandler(fh)
        except OSError:
            pass
    if stderr:
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(sh)
    return logger


def ring_of(logger: logging.Logger) -> Optional[RingHandler]:
    for h in logger.handlers:
        if isinstance(h, RingHandler):
            return h
    return None

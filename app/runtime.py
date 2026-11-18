"""Local operational guards. No credentials or message contents belong in logs."""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
from contextlib import contextmanager


class SafeLogFilter(logging.Filter):
    def __init__(self, token=""):
        super().__init__()
        self.token = token

    def filter(self, record):
        # Dependency exceptions/debug records can contain HTTP URLs or update payloads.
        if not record.name.startswith("app"):
            return False
        text = record.getMessage()
        if self.token:
            text = text.replace(self.token, "[REDACTED]")
        record.msg = re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{15,}\b", "[REDACTED]", text)
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def configure_logging(level, database_path, token):
    root = logging.getLogger()
    root.setLevel(level)
    directory = Path(database_path).parent / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(), RotatingFileHandler(
        directory / "collector.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")]
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()
    for handler in handlers:
        handler.addFilter(SafeLogFilter(token))
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)


@contextmanager
def instance_lock(database_path):
    """OS advisory lock, auto released after crash. Keep lock file; do not unlink it.

    This protects only processes using the same local database path. It does not
    coordinate different database paths, machines, network shares or bot tokens.
    """
    path = Path(database_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        import os
        if os.name == "nt":
            import msvcrt
            acquire = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        try:
            acquire()
        except OSError:
            raise RuntimeError("Another local instance is using this database.") from None
        try:
            yield
        finally:
            handle.seek(0)
            release()

"""Atomic, best-effort operational heartbeat; never stores Telegram data or secrets."""
from datetime import datetime, timezone
import json
from pathlib import Path
import os
import logging


def heartbeat_path(database_path):
    return Path(str(Path(database_path)) + '.heartbeat.json')


class Heartbeat:
    def __init__(self, database_path):
        self.path = heartbeat_path(database_path)
        self.state = {'started_at': self.now(), 'last_poll_at': None, 'state': 'starting'}

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    def write(self, state):
        self.state.update(state=state, updated_at=self.now())
        if state == 'polling':
            self.state['last_poll_at'] = self.now()
        temporary = self.path.with_name(self.path.name + f'.{os.getpid()}.tmp')
        try:
            temporary.write_text(json.dumps(self.state), encoding='utf-8')
            os.replace(temporary, self.path)
        except OSError:
            logging.getLogger(__name__).warning('Heartbeat could not be persisted.')
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

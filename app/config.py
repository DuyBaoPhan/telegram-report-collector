from dataclasses import dataclass
import logging
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_ids: frozenset[int]
    test_group_id: int
    log_level: str = 'INFO'
    database_path: Path = ROOT / 'data' / 'collector.db'

    report_rules_path: Path = ROOT / 'config' / 'report_rules.json'

    schedule_path: Path = ROOT / "config" / "schedule.json"

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / '.env', encoding='utf-8-sig')
        token = os.getenv('BOT_TOKEN', '').strip()
        if not token:
            raise ValueError('BOT_TOKEN chưa được cấu hình')
        try:
            admins = frozenset(int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip())
            group = int(os.getenv('TEST_GROUP_ID', '-1'))
        except ValueError:
            raise ValueError('ADMIN_IDS và TEST_GROUP_ID phải là số nguyên') from None
        if not admins or any(x <= 0 for x in admins):
            raise ValueError('ADMIN_IDS cần ít nhất một user ID dương')
        if group >= 0:
            raise ValueError('TEST_GROUP_ID phải âm')
        level = os.getenv('LOG_LEVEL', 'INFO').upper()
        if level not in logging.getLevelNamesMapping():
            raise ValueError('LOG_LEVEL không hợp lệ')
        path = Path(os.getenv('DATABASE_PATH', 'data/collector.db'))
        if not path.is_absolute():
            path = ROOT / path
        rules = Path(os.getenv('REPORT_RULES_PATH', 'config/report_rules.json'))
        if not rules.is_absolute():
            rules = ROOT / rules
        schedule = Path(os.getenv("SCHEDULE_PATH", "config/schedule.json"))
        if not schedule.is_absolute():
            schedule = ROOT / schedule
        return cls(token, admins, group, level, path, rules, schedule)

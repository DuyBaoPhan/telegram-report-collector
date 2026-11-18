import pytest
from app.config import Settings


@pytest.fixture(autouse=True)
def no_secret_file(monkeypatch):
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **kw: None)


def test_valid_config(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ADMIN_IDS", "1, 2")
    monkeypatch.setenv("TEST_GROUP_ID", "-123")
    assert Settings.from_env().admin_ids == frozenset({1, 2})


def test_invalid_group(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ADMIN_IDS", "1")
    monkeypatch.setenv("TEST_GROUP_ID", "123")
    with pytest.raises(ValueError):
        Settings.from_env()

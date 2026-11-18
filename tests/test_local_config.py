from app import config
from app.excel import template_manager


def test_local_defaults_and_explicit_path(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    folder = tmp_path / 'config'
    folder.mkdir()
    local = folder / 'report_rules.local.json'
    assert config.config_path('config/report_rules.json', 'config/report_rules.json') == folder / 'report_rules.json'
    local.write_text('{}')
    assert config.config_path('config/report_rules.json', 'config/report_rules.json') == local
    assert config.config_path(str(folder / 'report_rules.json'), 'config/report_rules.json') == local
    assert config.config_path('custom.json', 'config/report_rules.json') == tmp_path / 'custom.json'


def test_local_mapping_default_and_explicit_path(tmp_path, monkeypatch):
    monkeypatch.setattr(template_manager, 'ROOT', tmp_path)
    folder = tmp_path / 'config'
    folder.mkdir()
    local = folder / 'excel_mapping.local.json'
    local.write_text('invalid')
    import pytest
    import json
    with pytest.raises(json.JSONDecodeError):
        template_manager.TemplateManager()
    # An invalid local config must not silently fall back to public data.
    explicit = folder / 'excel_mapping.json'
    explicit.write_text('invalid')
    with pytest.raises(json.JSONDecodeError):
        template_manager.TemplateManager(explicit)

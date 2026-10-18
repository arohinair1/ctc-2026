"""Tests for live-bot configuration and secure credential loading."""

from __future__ import annotations

from ctc.execution.settings import LiveConfig, load_dotenv


def test_load_dotenv_parses_and_strips_quotes(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('CTC_API_KEY="abc123"\n# comment\nCTC_DRY_RUN=false\n\nEMPTY=\n')
    monkeypatch.delenv("CTC_API_KEY", raising=False)
    monkeypatch.delenv("CTC_DRY_RUN", raising=False)
    parsed = load_dotenv(env, override=True)
    assert parsed["CTC_API_KEY"] == "abc123"  # surrounding quotes stripped
    assert parsed["CTC_DRY_RUN"] == "false"


def test_load_dotenv_missing_file_is_noop(tmp_path):
    assert load_dotenv(tmp_path / "nope.env") == {}


def test_dry_run_defaults_true(monkeypatch):
    for k in ("CTC_DRY_RUN", "CTC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    cfg = LiveConfig.from_env(load_env_file=False)
    assert cfg.dry_run is True  # safety default


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("CTC_API_KEY", "key")
    monkeypatch.setenv("CTC_DRY_RUN", "false")
    monkeypatch.setenv("CTC_MAX_ORDERS_PER_MIN", "7")
    monkeypatch.setenv("CTC_MAX_GROSS_EXPOSURE", "123.0")
    cfg = LiveConfig.from_env(load_env_file=False)
    assert cfg.dry_run is False
    assert cfg.max_orders_per_min == 7
    assert cfg.max_gross_exposure == 123.0
    assert cfg.has_key()


def test_redacted_masks_key():
    cfg = LiveConfig(api_key="ABCDEFGHIJ")
    red = cfg.redacted()
    assert "ABCDEFGHIJ" not in str(red)
    assert red["api_key"] == "ABC…IJ"
    # repr also never leaks the key
    assert "ABCDEFGHIJ" not in repr(cfg)


def test_ws_url_derived_from_http():
    cfg = LiveConfig(http_url="https://example.org")
    assert cfg.ws_url == "wss://example.org/ws/v1/market-data"

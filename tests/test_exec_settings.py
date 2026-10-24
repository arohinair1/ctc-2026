"""Tests for live-bot configuration and secure credential loading."""

from __future__ import annotations

from ctc.execution.settings import DEFAULT_HTTP_URL, LiveConfig, load_dotenv


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


def test_rollout_configuration_is_conservative_and_disabled_by_default(monkeypatch):
    for key in (
        "CTC_AUTO_TRANSITION",
        "CTC_MAX_SESSION_LOSS",
        "CTC_INITIAL_MAX_GROSS_EXPOSURE",
        "CTC_INITIAL_MAX_ORDER_SIZE_FRAC",
        "CTC_INITIAL_ORDERS_PER_MIN",
        "CTC_INITIAL_MAX_ORDER_UNITS",
    ):
        monkeypatch.delenv(key, raising=False)
    cfg = LiveConfig.from_env(load_env_file=False)
    assert cfg.automatic_transition_enabled is False
    assert cfg.max_session_loss == 10_000
    assert cfg.max_initial_gross_exposure == 10_000
    assert cfg.initial_max_order_size_frac == 0.01
    assert cfg.initial_orders_per_min == 5
    assert cfg.initial_max_order_size_units == 1


def test_config_never_allows_upper_bounds_above_competition_limits():
    cfg = LiveConfig(
        max_order_size_frac=0.9,
        max_gross_exposure=9_000_000,
        max_orders_per_min=500,
        max_initial_gross_exposure=4_000_000,
        initial_max_order_size_frac=0.5,
        initial_orders_per_min=100,
    )
    assert cfg.max_order_size_frac == 0.25
    assert cfg.max_gross_exposure == 2_000_000
    assert cfg.max_orders_per_min == 50
    assert cfg.max_initial_gross_exposure == 2_000_000
    assert cfg.initial_max_order_size_frac == 0.25
    assert cfg.initial_orders_per_min == 50


def test_config_rejects_invalid_loss_limit():
    import pytest

    with pytest.raises(ValueError, match="positive finite"):
        LiveConfig(max_session_loss=0)


def test_redacted_masks_key():
    cfg = LiveConfig(api_key="ABCDEFGHIJ")
    red = cfg.redacted()
    assert "ABCDEFGHIJ" not in str(red)
    assert red["api_key"] == "<set>"
    # repr also never leaks the key
    assert "ABCDEFGHIJ" not in repr(cfg)


def test_ws_url_derived_from_http():
    cfg = LiveConfig(http_url="https://example.org")
    assert cfg.ws_url == "wss://example.org/ws/v1/market-data"


def test_default_host_is_organizer_confirmed_exchange():
    # starter kit derives WS as https -> wss + /ws/v1/market-data
    assert DEFAULT_HTTP_URL == "https://cornelltradingcompetition.org"
    cfg = LiveConfig()
    assert cfg.ws_url == "wss://cornelltradingcompetition.org/ws/v1/market-data"

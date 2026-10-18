"""Tests for the public case definition and derived config constants."""

from __future__ import annotations

from ctc.config import (
    ALL_SYMBOLS,
    ETFS,
    INSURERS,
    ISLAND_BUSINESSES,
    ISLANDS,
    REBUILDERS,
    START_PRICES,
    STOCKS,
    baskets,
    company_sites,
    limits,
    load_case,
)


def test_symbol_partition():
    assert len(ISLAND_BUSINESSES) == 5
    assert len(REBUILDERS) == 3
    assert len(INSURERS) == 2
    assert STOCKS == ISLAND_BUSINESSES + REBUILDERS + INSURERS
    assert len(STOCKS) == 10
    assert len(ETFS) == 2
    assert len(ALL_SYMBOLS) == 12
    # no duplicates
    assert len(set(ALL_SYMBOLS)) == 12


def test_start_prices_cover_all_symbols():
    assert set(START_PRICES) == set(ALL_SYMBOLS)
    assert all(p > 0 for p in START_PRICES.values())


def test_case_loads_and_is_cached():
    case = load_case()
    assert load_case() is case  # lru_cache
    assert case["name"] == "Hurricane Season"
    assert set(case["islands"]) == set(ISLANDS)


def test_limits_match_case_and_packet():
    lim = limits()
    assert set(lim) == set(ALL_SYMBOLS)
    # spot check against the case packet (section 3)
    assert lim["PORT"] == 200
    assert lim["FERRY"] == 800
    assert lim["ARCH"] == 50
    assert lim["REBLD"] == 100


def test_baskets_match_packet():
    b = baskets()
    assert b["ARCH"] == {"PORT": 1, "ISLE": 1, "AQUA": 2, "GRID": 1, "FERRY": 2, "HAVEN": 1}
    assert b["REBLD"] == {"RECON": 2, "MAT": 2, "GEN": 1}


def test_company_sites_structure():
    sites = company_sites()
    # island businesses have sites with coordinates and weights
    for sym in ISLAND_BUSINESSES:
        assert sites[sym], f"{sym} should have sites"
        for s in sites[sym]:
            assert {"x", "y", "weight"} <= set(s)
        # site weights sum to ~1.0
        assert abs(sum(s["weight"] for s in sites[sym]) - 1.0) < 1e-6
    # rebuilders and insurers have no physical sites
    for sym in REBUILDERS + INSURERS:
        assert sites[sym] == []

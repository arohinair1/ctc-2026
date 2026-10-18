"""Tests for the storm wind-field and outage model."""

from __future__ import annotations

import math

from ctc.models.wind import (
    company_outage_metric,
    group_storm_obs,
    site_closed,
    wind_at,
)
from tests.conftest import make_storm_obs


def _obs(x, y, radius=80.0, intensity=1.0):
    return {"x": x, "y": y, "radius": radius, "intensity": intensity}


def test_wind_at_center_equals_intensity():
    obs = _obs(100.0, 100.0, radius=50.0, intensity=0.8)
    assert wind_at(100.0, 100.0, obs) == 0.8


def test_wind_decreases_with_distance():
    obs = _obs(0.0, 0.0, radius=100.0, intensity=1.0)
    near = wind_at(10.0, 0.0, obs)
    far = wind_at(200.0, 0.0, obs)
    assert near > far
    # exponential falloff: wind = intensity * exp(-dist/radius)
    assert math.isclose(wind_at(100.0, 0.0, obs), math.exp(-1.0), rel_tol=1e-9)


def test_wind_zero_radius_is_zero():
    assert wind_at(0.0, 0.0, _obs(0.0, 0.0, radius=0.0)) == 0.0
    assert wind_at(0.0, 0.0, _obs(0.0, 0.0, radius=-5.0)) == 0.0


def test_site_closed_threshold():
    obs = _obs(0.0, 0.0, radius=100.0, intensity=1.0)
    # at the center wind == 1.0 >= threshold -> closed
    assert site_closed(0.0, 0.0, obs, threshold=0.39)
    # very far -> open
    assert not site_closed(1000.0, 0.0, obs, threshold=0.39)
    # exactly at threshold boundary is closed (>=)
    assert site_closed(0.0, 0.0, obs, threshold=1.0)
    assert not site_closed(0.0, 0.0, obs, threshold=1.0001)


def test_outage_metric_zero_when_storm_far():
    sites = [{"x": 0.0, "y": 0.0, "weight": 1.0}]
    far_obs = [make_storm_obs(1, age=a, x=5000.0, y=5000.0) for a in range(0, 20, 2)]
    assert company_outage_metric({1: far_obs}, sites) == 0.0


def test_outage_metric_accumulates_closed_time():
    sites = [{"x": 0.0, "y": 0.0, "weight": 1.0}]
    # 5 observations directly on the site (wind == intensity == 1.0 >= threshold)
    obs = [make_storm_obs(1, age=a, x=0.0, y=0.0) for a in (0, 2, 4, 6, 8)]
    metric = company_outage_metric({1: obs}, sites)
    # weight(1.0) * 5 closed obs * dt(2.0) = 10
    assert metric == 10.0


def test_outage_metric_weighted_and_summed_over_rounds():
    sites = [{"x": 0.0, "y": 0.0, "weight": 0.5}]
    obs_r1 = [make_storm_obs(1, age=a, x=0.0, y=0.0) for a in (0, 2, 4)]
    obs_r2 = [make_storm_obs(2, age=a, x=0.0, y=0.0) for a in (0, 2)]
    metric = company_outage_metric({1: obs_r1, 2: obs_r2}, sites)
    # 0.5*3*2 + 0.5*2*2 = 3 + 2 = 5
    assert metric == 5.0


def test_group_storm_obs_groups_by_round():
    feed = [
        make_storm_obs(1, age=0, x=1, y=1),
        make_storm_obs(1, age=2, x=1, y=1),
        make_storm_obs(2, age=0, x=1, y=1),
        {"type": "advisory", "round": 1, "outlook": "direct"},
    ]
    grouped = group_storm_obs(feed)
    assert set(grouped) == {1, 2}
    assert len(grouped[1]) == 2
    assert len(grouped[2]) == 1

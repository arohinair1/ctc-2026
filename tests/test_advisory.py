"""Tests for advisory / wire information-quality analysis.

Uses small hand-built fake seasons so the leakage-aware scoring can be checked exactly.
"""

from __future__ import annotations

from ctc.forecasts.advisory import (
    accuracy,
    confusion_matrix,
    earliness,
    hurricane_condition_calibration,
    truth_at_age,
    wire_adds_information,
)
from tests.conftest import (
    FakeHistory,
    FakeSeason,
    make_advisory,
    make_impact,
    make_manifest_storm,
)


def test_truth_at_age_before_and_after_flip():
    storm = make_manifest_storm(1, pattern="direct", turned_erratic_at=200.0)
    assert truth_at_age(storm, 100.0) == "direct"
    assert truth_at_age(storm, 200.0) == "erratic"
    assert truth_at_age(storm, 300.0) == "erratic"
    # never flips
    storm2 = make_manifest_storm(1, pattern="recurve", turned_erratic_at=None)
    assert truth_at_age(storm2, 10_000) == "recurve"


def _one_season():
    storms = [
        make_manifest_storm(1, pattern="direct", turned_erratic_at=None),
        make_manifest_storm(2, pattern="recurve", turned_erratic_at=150.0),
    ]
    feed = [
        # round 1: official always correct, wire always wrong
        make_advisory(1, "direct", age=30, source="official"),
        make_advisory(1, "direct", age=90, source="official"),
        make_advisory(1, "stall", age=40, source="wire"),
        # round 2: official correct until flip, then names erratic at age 150
        make_advisory(2, "recurve", age=30, source="official"),
        make_advisory(2, "erratic", age=150, source="official"),
    ]
    return FakeSeason(1, storms, feed)


def test_accuracy_overall_and_counts():
    hist = FakeHistory([_one_season()])
    res = accuracy(hist)
    # official: 4 advisories, all correct against truth-at-age
    assert res.overall["official"] == 1.0
    assert res.counts["official"] == 4
    # wire: 1 advisory, wrong
    assert res.overall["wire"] == 0.0
    assert res.counts["wire"] == 1


def test_confusion_matrix_counts():
    hist = FakeHistory([_one_season()])
    mat = confusion_matrix(hist, "official")
    assert mat["direct"]["direct"] == 2
    assert mat["recurve"]["recurve"] == 1
    assert mat["erratic"]["erratic"] == 1
    # wire predicted stall when truth was direct
    matw = confusion_matrix(hist, "wire")
    assert matw["direct"]["stall"] == 1


def test_earliness_identifies_final_pattern():
    # a storm where official locks onto the final pattern from age 30 onward
    storms = [make_manifest_storm(1, pattern="chain", turned_erratic_at=None)]
    feed = [
        make_advisory(1, "chain", age=30, source="official"),
        make_advisory(1, "chain", age=90, source="official"),
    ]
    hist = FakeHistory([FakeSeason(1, storms, feed)])
    early = earliness(hist)
    assert early["official"]["mean_age"] == 30.0
    assert early["official"]["n_identified"] == 1
    # wire never speaks -> counted as never
    assert early["wire"]["never_frac"] == 1.0


def test_calibration_bins_align():
    storms = [make_manifest_storm(1, pattern="direct", turned_erratic_at=None)]
    feed = [
        make_advisory(
            1,
            "direct",
            age=30,
            source="official",
            hurricane_conditions={"palm_atoll": 0.9, "cascadia_prime": 0.05},
        ),
        # palm_atoll actually had an impact; cascadia did not
        make_impact(1, "palm_atoll", age=100),
    ]
    hist = FakeHistory([FakeSeason(1, storms, feed)])
    cal = hurricane_condition_calibration(hist, n_bins=10)
    assert len(cal.predicted) == 10
    assert len(cal.observed) == 10
    # high-prob bin (0.9) observed realized=1
    assert cal.observed[9] == 1.0
    # low-prob bin (0.05) observed realized=0
    assert cal.observed[0] == 0.0


def test_wire_adds_information_fields():
    hist = FakeHistory([_one_season()])
    info = wire_adds_information(hist)
    assert set(info) >= {
        "official_accuracy",
        "wire_accuracy",
        "agree_frac",
        "n_pairs",
    }
    # in the fake season, the only wire message (age 40) has a prior official (age 30)
    assert info["n_pairs"] == 1

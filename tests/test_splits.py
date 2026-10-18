"""Tests for deterministic train/val/test season splits."""

from __future__ import annotations

from ctc.data.splits import split_seasons


def test_split_is_deterministic():
    nums = list(range(1, 101))
    a = split_seasons(nums)
    b = split_seasons(nums)
    assert a.as_dict() == b.as_dict()


def test_split_partitions_without_overlap():
    nums = list(range(1, 201))
    s = split_seasons(nums)
    all_split = s.train + s.val + s.test
    assert sorted(all_split) == nums  # covers everything exactly once
    assert not (set(s.train) & set(s.val))
    assert not (set(s.train) & set(s.test))
    assert not (set(s.val) & set(s.test))


def test_split_fractions_approximate():
    nums = list(range(1, 601))
    s = split_seasons(nums, train_frac=0.6, val_frac=0.2)
    assert abs(len(s.train) - 360) <= 1
    assert abs(len(s.val) - 120) <= 1
    assert abs(len(s.test) - 120) <= 1


def test_split_seed_changes_partition():
    nums = list(range(1, 101))
    a = split_seasons(nums, seed=1)
    b = split_seasons(nums, seed=2)
    assert a.as_dict() != b.as_dict()


def test_each_split_is_sorted():
    nums = list(range(1, 101))
    s = split_seasons(nums)
    assert s.train == sorted(s.train)
    assert s.val == sorted(s.val)
    assert s.test == sorted(s.test)

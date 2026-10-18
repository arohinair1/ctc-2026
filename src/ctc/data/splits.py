"""Deterministic train / validation / test splits by whole season.

Splitting by entire seasons (never by events within a season) is essential to
prevent look-ahead leakage: a model is never evaluated on a season whose outcomes
influenced its parameters.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

DEFAULT_SEED = 20260118


@dataclass
class SeasonSplit:
    train: list[int]
    val: list[int]
    test: list[int]

    def as_dict(self) -> dict[str, list[int]]:
        return {"train": self.train, "val": self.val, "test": self.test}


def split_seasons(
    season_numbers: list[int],
    train_frac: float = 0.6,
    val_frac: float = 0.2,
    seed: int = DEFAULT_SEED,
) -> SeasonSplit:
    """Shuffle season numbers with a fixed seed and partition into train/val/test."""
    rng = np.random.default_rng(seed)
    order = list(season_numbers)
    rng.shuffle(order)
    n = len(order)
    n_train = int(round(train_frac * n))
    n_val = int(round(val_frac * n))
    train = sorted(order[:n_train])
    val = sorted(order[n_train : n_train + n_val])
    test = sorted(order[n_train + n_val :])
    return SeasonSplit(train, val, test)

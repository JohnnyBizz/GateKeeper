"""Chronological data splits — specification section 23.

Three rules, and the first one is the one people break.

**Never shuffle.** A time series split at random leaks the future into the
training set through autocorrelation: tomorrow's bar sits in-sample while
today's is held out. Every split here is contiguous and in time order, and
there is no option to randomise, because the option would eventually be
used.

**Never overlap.** A bar in two segments is a bar scored twice, and the
held-out set stops being held out.

**Count the looks at out-of-sample data.** A strategy tuned until the
out-of-sample numbers improve has no out-of-sample data left; it has a
slower in-sample fit. There is no way to enforce that in code, so this
module does the next best thing and keeps a count, so the number of looks
is at least visible to whoever reads the result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterator, Sequence

from gtcc.domain.market_data import Bar
from gtcc.domain.money import D


class SplitError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Split:
    """One chronological partition of a bar series."""

    in_sample: tuple[Bar, ...]
    validation: tuple[Bar, ...]
    out_of_sample: tuple[Bar, ...]

    def describe(self) -> str:
        return (
            f"{len(self.in_sample)} in-sample, "
            f"{len(self.validation)} validation, "
            f"{len(self.out_of_sample)} out-of-sample bars, in that order"
        )

    @property
    def total(self) -> int:
        return len(self.in_sample) + len(self.validation) + len(self.out_of_sample)


def split(
    bars: Sequence[Bar],
    *,
    in_sample: Decimal | str = "0.6",
    validation: Decimal | str = "0.2",
) -> Split:
    """Partition *bars* chronologically. Out-of-sample is the remainder.

    Fractions are of the whole series. The remainder is out-of-sample
    rather than being computed from a third fraction, so the three can
    never silently fail to add up to everything.
    """
    first = D(in_sample)
    second = D(validation)
    if first <= 0 or second < 0:
        raise SplitError("the in-sample fraction must be positive")
    if first + second >= D(1):
        raise SplitError(
            f"in-sample {first} plus validation {second} leaves no out-of-sample "
            "data; a split with nothing held out is not a split"
        )

    ordered = sorted(bars, key=lambda bar: bar.timestamp)
    if len(ordered) != len(bars):  # pragma: no cover - defensive
        raise SplitError("bars could not be ordered")
    count = len(ordered)
    if count < 3:
        raise SplitError(f"{count} bars cannot be split three ways")

    end_in = int(count * first)
    end_val = end_in + int(count * second)
    if end_in == 0 or end_val == end_in or end_val >= count:
        raise SplitError(
            f"{count} bars split {first}/{second} leaves an empty segment; "
            "use a longer series or different fractions"
        )

    return Split(
        in_sample=tuple(ordered[:end_in]),
        validation=tuple(ordered[end_in:end_val]),
        out_of_sample=tuple(ordered[end_val:]),
    )


@dataclass(frozen=True, slots=True)
class Window:
    """One walk-forward step: train on the past, test on what follows."""

    index: int
    train: tuple[Bar, ...]
    test: tuple[Bar, ...]


def walk_forward(
    bars: Sequence[Bar], *, train_size: int, test_size: int, step: int | None = None
) -> Iterator[Window]:
    """Rolling windows, each testing only on bars after its training set.

    The test window always follows its training window in time. An
    anchored variant (training set growing rather than rolling) is a
    different method and would be a different function; conflating them
    behind a flag makes it impossible to tell from a result which was run.
    """
    if train_size <= 0 or test_size <= 0:
        raise SplitError("train and test sizes must both be positive")
    ordered = sorted(bars, key=lambda bar: bar.timestamp)
    stride = step or test_size
    if stride <= 0:
        raise SplitError("step must be positive")

    index = 0
    start = 0
    while start + train_size + test_size <= len(ordered):
        train = tuple(ordered[start : start + train_size])
        test = tuple(ordered[start + train_size : start + train_size + test_size])
        yield Window(index=index, train=train, test=test)
        index += 1
        start += stride


@dataclass
class OutOfSampleLedger:
    """How many times a strategy's held-out data has been looked at.

    Nothing here can stop somebody looking again. The point is that the
    count travels with the result, so a strategy promoted on its fourth
    look at out-of-sample data cannot present that as a first look.
    """

    looks: dict[str, int] = field(default_factory=dict)

    def record(self, strategy: str) -> int:
        self.looks[strategy] = self.looks.get(strategy, 0) + 1
        return self.looks[strategy]

    def count(self, strategy: str) -> int:
        return self.looks.get(strategy, 0)

    def warning_for(self, strategy: str) -> str:
        count = self.count(strategy)
        if count <= 1:
            return ""
        return (
            f"{strategy} has now been measured against its out-of-sample data "
            f"{count} times. After the first look it is no longer out-of-sample "
            "in any useful sense: each further look tunes the strategy to it. "
            "Treat these numbers as in-sample."
        )

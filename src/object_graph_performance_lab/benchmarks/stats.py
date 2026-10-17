"""Latency summaries. Inputs are nanosecond samples from time.perf_counter_ns."""

from __future__ import annotations

import math
import statistics


def percentile_linear(sorted_values: list[float], percent: float) -> float:
    """Linear interpolation between closest ranks.

    ``percent`` is in ``[0, 100]``. For one sample, that sample is returned.
    For ``percent == 50`` this matches ``statistics.median`` on a sorted copy.
    Rank is ``(percent / 100) * (n - 1)``.
    """
    if not sorted_values:
        raise ValueError("percentile requires at least one sample")
    if percent < 0 or percent > 100:
        raise ValueError("percent must be between 0 and 100")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (percent / 100) * (len(sorted_values) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return float(sorted_values[low])
    weight = rank - low
    return float(sorted_values[low]) * (1 - weight) + float(sorted_values[high]) * weight


def summarize_ns(samples_ns: list[int]) -> dict[str, float | int | list[float]]:
    """Return millisecond statistics. Requires at least two samples.

    Standard deviation is the sample standard deviation (divide by n - 1).
    The returned ``samples_ms`` list preserves iteration order.
    """
    if len(samples_ns) < 2:
        raise ValueError("at least two measured iterations are required")
    samples_ms = [sample / 1_000_000 for sample in samples_ns]
    ordered = sorted(samples_ms)
    return {
        "count": len(samples_ms),
        "median_ms": float(statistics.median(samples_ms)),
        "p95_ms": percentile_linear(ordered, 95),
        "min_ms": ordered[0],
        "max_ms": ordered[-1],
        "stdev_ms": float(statistics.stdev(samples_ms)),
        "samples_ms": samples_ms,
    }


def median_ratio(numerator: float, denominator: float) -> float:
    """Divide two medians. Refuses a zero denominator instead of reporting infinity."""
    if denominator == 0:
        raise ValueError("refusing a ratio with a zero median denominator")
    return numerator / denominator

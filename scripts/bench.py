#!/usr/bin/env python3
"""
Host-vs-VM benchmark harness for Beetroot CI (issue #50).

This is the *analysis* half of the nightly benchmark lane. The workflow
(``.github/workflows/benchmark.yml``) does the actual measuring — it boots the
host-binder and ``binder: vm`` backends on the same runner in the same run and
records each wall-time via :func:`measure`. This module then aggregates those
samples, computes the host-vs-vm **ratio** (which cancels per-runner hardware
noise far better than absolute seconds on shared-tenancy runners), compares
against an optional committed baseline, and renders a Markdown trend table for
the job step-summary.

Design constraints, straight from the issue:

* **Track, don't gate.** :func:`report` *never* exits non-zero on a slow
  result — a regression only emits a GitHub ``::warning::`` annotation. Hard
  perf thresholds flake on shared runners, so benchmarking lives in its own
  nightly workflow and is never a per-PR gate.
* **Ratio over absolutes.** The host-vs-vm ratio is the headline number; raw
  seconds are reported too, but the ratio is what we trend.
* **Regression alert at 2x.** A backend/metric that is more than
  :data:`DEFAULT_REGRESSION_FACTOR` times slower than the baseline is flagged.

The split (this pure-Python analysis vs the boot-timing shell in the workflow)
keeps the load-bearing logic unit-testable without a runner: see
``tests/test_bench.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

# A backend/metric that is more than this many times slower than the baseline
# is flagged as a regression (GitHub ``::warning::`` only — never a failure).
DEFAULT_REGRESSION_FACTOR = 2.0

# Recognised backends and metrics. These are advisory — ``measure`` does not
# reject unknown values (a new backend shouldn't need a code change to be
# recorded) — but they document the schema and drive a stable table ordering.
KNOWN_BACKENDS = ("host", "vm-kvm", "vm-tcg")
KNOWN_METRICS = ("compile_seconds", "boot_seconds", "postboot_seconds")

# The backend every ratio is taken against. Boot/post-boot ratios are
# ``<backend> / host`` so the cheap host path is the 1.0 reference.
RATIO_BASELINE_BACKEND = "host"


@dataclass(frozen=True)
class Sample:
    """
    One wall-time measurement of a single backend running a single metric.

    Attributes:
        backend: The backend measured (e.g. ``host``, ``vm-tcg``, ``vm-kvm``).
        metric: The workload measured (e.g. ``boot_seconds``).
        seconds: The measured wall-time in seconds.
    """

    backend: str
    metric: str
    seconds: float


@dataclass(frozen=True)
class Regression:
    """
    A backend/metric that ran materially slower than the committed baseline.

    Attributes:
        backend: The backend that regressed.
        metric: The metric that regressed.
        current: The current aggregated wall-time (seconds).
        baseline: The baseline aggregated wall-time (seconds).
        factor: ``current / baseline`` — how many times slower the run is.
    """

    backend: str
    metric: str
    current: float
    baseline: float
    factor: float


def _samples_to_jsonable(samples: list[Sample]) -> list[dict[str, object]]:
    """
    Convert samples to a list of plain dicts for :func:`json.dump`.

    Args:
        samples: The samples to serialise.

    Returns:
        One dict per sample, with ``backend`` / ``metric`` / ``seconds`` keys.
    """
    return [asdict(s) for s in samples]


def load_samples(path: Path) -> list[Sample]:
    """
    Read a samples file written by :func:`measure`.

    The file is a JSON object with a top-level ``samples`` list; a bare list is
    also accepted so a hand-written corpus needs no wrapper.

    Args:
        path: The samples JSON file.

    Returns:
        The parsed samples (empty list if the file is missing).
    """
    if not path.exists():
        return []
    raw = json.loads(path.read_text())
    rows = raw["samples"] if isinstance(raw, dict) else raw
    return [
        Sample(backend=str(r["backend"]), metric=str(r["metric"]), seconds=float(r["seconds"]))
        for r in rows
    ]


def save_samples(path: Path, samples: list[Sample], *, runner: str | None = None) -> None:
    """
    Write samples to ``path`` as a JSON object with metadata.

    Args:
        path: The destination file (parent directories are created).
        samples: The samples to persist.
        runner: An optional runner label recorded alongside the samples (so a
            cross-runner artifact stays self-describing).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "runner": runner or os.environ.get("RUNNER_NAME", "unknown"),
        "samples": _samples_to_jsonable(samples),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def aggregate(samples: list[Sample]) -> dict[tuple[str, str], float]:
    """
    Collapse repeated samples into one mean wall-time per backend/metric.

    Measuring the same backend/metric more than once in a run is encouraged
    (it shrinks the confidence interval); this folds those repeats into the
    arithmetic mean keyed by ``(backend, metric)``.

    Args:
        samples: The raw samples.

    Returns:
        A mapping from ``(backend, metric)`` to the mean wall-time in seconds.
    """
    grouped: dict[tuple[str, str], list[float]] = {}
    for s in samples:
        grouped.setdefault((s.backend, s.metric), []).append(s.seconds)
    return {key: statistics.fmean(values) for key, values in grouped.items()}


def compute_ratios(aggregated: dict[tuple[str, str], float]) -> dict[str, dict[str, float]]:
    """
    Compute each backend's wall-time as a ratio of the host baseline.

    For every metric that has a :data:`RATIO_BASELINE_BACKEND` measurement, the
    other backends' wall-times are divided by it — the hardware-noise-cancelling
    number the issue asks us to trend. Metrics with no host sample are skipped
    (there is nothing to divide by).

    Args:
        aggregated: The per-backend/metric means from :func:`aggregate`.

    Returns:
        ``{metric: {backend: ratio}}``. The baseline backend is included with a
        ratio of ``1.0`` for context.
    """
    metrics = {metric for _, metric in aggregated}
    ratios: dict[str, dict[str, float]] = {}
    for metric in metrics:
        base = aggregated.get((RATIO_BASELINE_BACKEND, metric))
        if base is None or base == 0:
            continue
        per_backend = {
            backend: seconds / base for (backend, m), seconds in aggregated.items() if m == metric
        }
        ratios[metric] = per_backend
    return ratios


def detect_regressions(
    current: dict[tuple[str, str], float],
    baseline: dict[tuple[str, str], float],
    *,
    factor: float = DEFAULT_REGRESSION_FACTOR,
) -> list[Regression]:
    """
    Flag backend/metrics that are more than ``factor`` times slower than baseline.

    Only keys present in *both* maps are compared — a newly added backend or a
    dropped baseline entry is silently ignored rather than reported as noise.

    Args:
        current: This run's aggregated means.
        baseline: The committed baseline's aggregated means.
        factor: The slowdown multiple above which a result is a regression.

    Returns:
        The regressions, sorted by descending slowdown factor.
    """
    found: list[Regression] = []
    for key, now in current.items():
        before = baseline.get(key)
        if before is None or before == 0:
            continue
        ratio = now / before
        if ratio > factor:
            backend, metric = key
            found.append(
                Regression(
                    backend=backend,
                    metric=metric,
                    current=now,
                    baseline=before,
                    factor=ratio,
                )
            )
    return sorted(found, key=lambda r: r.factor, reverse=True)


def _ordered(values: set[str], preferred: tuple[str, ...]) -> list[str]:
    """
    Order ``values`` by ``preferred`` first, then any extras alphabetically.

    Args:
        values: The set of labels to order.
        preferred: The canonical ordering for known labels.

    Returns:
        A stable, human-friendly ordering.
    """
    known = [v for v in preferred if v in values]
    extra = sorted(values - set(preferred))
    return known + extra


def render_markdown(
    aggregated: dict[tuple[str, str], float],
    ratios: dict[str, dict[str, float]],
    regressions: list[Regression],
) -> str:
    """
    Render the run as a Markdown trend report for the GitHub step-summary.

    Args:
        aggregated: Per-backend/metric mean wall-times.
        ratios: Per-metric host-vs-backend ratios from :func:`compute_ratios`.
        regressions: Any regressions from :func:`detect_regressions`.

    Returns:
        A Markdown string (absolute seconds table + ratio table + a regression
        callout, or an "all clear" note).
    """
    lines: list[str] = ["## Host-vs-VM benchmark", ""]

    backends = _ordered({b for b, _ in aggregated}, KNOWN_BACKENDS)
    metrics = _ordered({m for _, m in aggregated}, KNOWN_METRICS)

    lines.append("### Wall-time (seconds)")
    lines.append("")
    lines.append("| metric | " + " | ".join(backends) + " |")
    lines.append("| --- | " + " | ".join("---" for _ in backends) + " |")
    for metric in metrics:
        cells = [_fmt_seconds(aggregated.get((b, metric))) for b in backends]
        lines.append(f"| {metric} | " + " | ".join(cells) + " |")

    lines.append("")
    lines.append(f"### Ratio vs `{RATIO_BASELINE_BACKEND}`")
    lines.append("")
    lines.append("| metric | " + " | ".join(backends) + " |")
    lines.append("| --- | " + " | ".join("---" for _ in backends) + " |")
    for metric in metrics:
        per_backend = ratios.get(metric, {})
        cells = [_fmt_ratio(per_backend.get(b)) for b in backends]
        lines.append(f"| {metric} | " + " | ".join(cells) + " |")

    lines.append("")
    if regressions:
        lines.append(f"### :warning: {len(regressions)} regression(s)")
        lines.append("")
        lines.append("| backend | metric | baseline | current | factor |")
        lines.append("| --- | --- | --- | --- | --- |")
        lines.extend(
            f"| {r.backend} | {r.metric} | {r.baseline:.1f}s | {r.current:.1f}s | {r.factor:.2f}x |"
            for r in regressions
        )
    else:
        lines.append("No regressions over the baseline. :white_check_mark:")
    lines.append("")
    return "\n".join(lines)


def _fmt_seconds(value: float | None) -> str:
    """Format a wall-time cell, or ``-`` when the backend wasn't measured."""
    return f"{value:.1f}" if value is not None else "-"


def _fmt_ratio(value: float | None) -> str:
    """Format a ratio cell, or ``-`` when there's no comparable measurement."""
    return f"{value:.2f}x" if value is not None else "-"


def measure(args: argparse.Namespace) -> int:
    """
    Time a command's wall-clock and append the result to the samples file.

    The command is run with output inherited (so the workflow log shows the
    boot progress live). A non-zero command exit is propagated — a failed boot
    *should* fail the measure step so the workflow surfaces it — and no sample
    is recorded in that case.

    Args:
        args: Parsed CLI args (``backend``, ``metric``, ``samples_file``,
            ``command``).

    Returns:
        The wrapped command's exit code.
    """
    samples_file = Path(args.samples_file)
    start = time.monotonic()
    completed = subprocess.run(args.command, check=False)  # noqa: S603  # argv is operator-supplied in the workflow; this harness times whatever it's handed
    elapsed = time.monotonic() - start
    if completed.returncode != 0:
        print(
            f"bench: command failed (exit {completed.returncode}); not recording a sample",
            file=sys.stderr,
        )
        return completed.returncode
    _append_sample(samples_file, args.backend, args.metric, elapsed, runner=args.runner)
    return 0


def record(args: argparse.Namespace) -> int:
    """
    Append a pre-measured wall-time to the samples file.

    The workflow often times a boot itself (a ``date``-delta around a boot-wait
    loop is simpler in shell than wrapping the whole thing as one command), then
    hands the number here. This is :func:`measure` minus the timing.

    Args:
        args: Parsed CLI args (``backend``, ``metric``, ``seconds``,
            ``samples_file``, ``runner``).

    Returns:
        Always ``0``.
    """
    _append_sample(
        Path(args.samples_file), args.backend, args.metric, args.seconds, runner=args.runner
    )
    return 0


def _append_sample(
    samples_file: Path, backend: str, metric: str, seconds: float, *, runner: str | None
) -> None:
    """
    Append one sample to ``samples_file`` and echo what was recorded.

    Args:
        samples_file: The JSON samples file (created if absent).
        backend: The backend label.
        metric: The metric label.
        seconds: The measured wall-time.
        runner: An optional runner label persisted with the samples.
    """
    existing = load_samples(samples_file)
    existing.append(Sample(backend=backend, metric=metric, seconds=seconds))
    save_samples(samples_file, existing, runner=runner)
    print(f"bench: recorded {backend}/{metric} = {seconds:.1f}s -> {samples_file}")


def report(args: argparse.Namespace) -> int:
    """
    Aggregate samples, compute ratios, render the summary, and alert on regressions.

    Writes the machine-readable results JSON (always) and the Markdown summary
    (to ``--summary``, else ``$GITHUB_STEP_SUMMARY``, else stdout). Regressions
    are emitted as GitHub ``::warning::`` annotations but **never** change the
    exit code — benchmarking tracks, it does not gate.

    Args:
        args: Parsed CLI args (``samples_file``, ``baseline``, ``results_out``,
            ``summary``, ``factor``).

    Returns:
        Always ``0``.
    """
    samples = load_samples(Path(args.samples_file))
    aggregated = aggregate(samples)
    ratios = compute_ratios(aggregated)

    regressions: list[Regression] = []
    if args.baseline:
        baseline_path = Path(args.baseline)
        if baseline_path.exists():
            baseline = aggregate(load_samples(baseline_path))
            regressions = detect_regressions(aggregated, baseline, factor=args.factor)
        else:
            print(f"bench: baseline {baseline_path} not found; skipping regression check")

    if args.results_out:
        results_path = Path(args.results_out)
        results_path.parent.mkdir(parents=True, exist_ok=True)
        results_path.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "aggregated": [
                        {"backend": b, "metric": m, "seconds": v}
                        for (b, m), v in sorted(aggregated.items())
                    ],
                    "ratios": ratios,
                    "regressions": [asdict(r) for r in regressions],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

    markdown = render_markdown(aggregated, ratios, regressions)
    summary_target = args.summary or os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_target:
        with Path(summary_target).open("a", encoding="utf-8") as fh:
            fh.write(markdown)
    else:
        print(markdown)

    for r in regressions:
        print(
            f"::warning title=Benchmark regression::{r.backend}/{r.metric} is "
            f"{r.factor:.2f}x slower than baseline ({r.current:.1f}s vs {r.baseline:.1f}s)"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    """
    Build the ``bench.py`` argument parser.

    Returns:
        A parser with the ``measure`` and ``report`` subcommands wired to their
        handlers via ``set_defaults(func=...)``.
    """
    parser = argparse.ArgumentParser(description="Beetroot host-vs-VM benchmark harness")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    m = sub.add_parser("measure", help="time a command and append a sample")
    m.add_argument("--backend", required=True, help="backend label (e.g. host, vm-tcg)")
    m.add_argument("--metric", required=True, help="metric label (e.g. boot_seconds)")
    m.add_argument("--samples-file", required=True, help="JSON samples file to append to")
    m.add_argument("--runner", default=None, help="runner label recorded with the sample")
    m.add_argument("command", nargs=argparse.REMAINDER, help="the command to time (after --)")
    m.set_defaults(func=measure)

    rec = sub.add_parser("record", help="append a pre-measured wall-time")
    rec.add_argument("--backend", required=True, help="backend label (e.g. host, vm-tcg)")
    rec.add_argument("--metric", required=True, help="metric label (e.g. boot_seconds)")
    rec.add_argument("--seconds", required=True, type=float, help="the wall-time to record")
    rec.add_argument("--samples-file", required=True, help="JSON samples file to append to")
    rec.add_argument("--runner", default=None, help="runner label recorded with the sample")
    rec.set_defaults(func=record)

    r = sub.add_parser("report", help="aggregate samples and render the trend summary")
    r.add_argument("--samples-file", required=True, help="JSON samples file to read")
    r.add_argument("--baseline", default=None, help="optional committed baseline samples file")
    r.add_argument("--results-out", default=None, help="write machine-readable results JSON here")
    r.add_argument("--summary", default=None, help="Markdown path (else $GITHUB_STEP_SUMMARY)")
    r.add_argument(
        "--factor",
        type=float,
        default=DEFAULT_REGRESSION_FACTOR,
        help="slowdown multiple above which a result is flagged",
    )
    r.set_defaults(func=report)
    return parser


def main(argv: list[str] | None = None) -> int:
    """
    Parse arguments and dispatch to the selected subcommand.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        The subcommand's exit code.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = args.func
    if args.subcommand == "measure" and args.command and args.command[0] == "--":
        args.command = args.command[1:]
    result = handler(args)
    return int(result)


if __name__ == "__main__":
    raise SystemExit(main())

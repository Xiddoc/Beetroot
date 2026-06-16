"""
Unit tests for ``scripts/bench.py`` (issue #50).

These exercise the pure-Python analysis half of the benchmark harness — the
aggregation, ratio, regression, and Markdown logic that runs identically with
or without a runner — plus the ``measure``/``report`` CLI driving a trivial
command. No real boot is timed; the slow host-vs-vm measuring lives in the
nightly workflow.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "bench.py"
_SPEC = importlib.util.spec_from_file_location("bench", _SCRIPT_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
bench: ModuleType = importlib.util.module_from_spec(_SPEC)
# Register before exec so dataclass field resolution can find the module
# (dataclasses look up ``sys.modules[cls.__module__]`` under ``from __future__
# import annotations``).
sys.modules["bench"] = bench
_SPEC.loader.exec_module(bench)


def _samples(*rows: tuple[str, str, float]) -> list[object]:
    return [bench.Sample(backend=b, metric=m, seconds=s) for b, m, s in rows]


def test_aggregate_means_repeated_samples() -> None:
    agg = bench.aggregate(
        _samples(
            ("host", "boot_seconds", 10.0),
            ("host", "boot_seconds", 20.0),
            ("vm-tcg", "boot_seconds", 100.0),
        )
    )
    assert agg[("host", "boot_seconds")] == 15.0
    assert agg[("vm-tcg", "boot_seconds")] == 100.0


def test_compute_ratios_divides_by_host_baseline() -> None:
    agg = {
        ("host", "boot_seconds"): 10.0,
        ("vm-tcg", "boot_seconds"): 120.0,
        ("vm-kvm", "boot_seconds"): 12.0,
    }
    ratios = bench.compute_ratios(agg)
    assert ratios["boot_seconds"]["host"] == 1.0
    assert ratios["boot_seconds"]["vm-tcg"] == 12.0
    assert ratios["boot_seconds"]["vm-kvm"] == pytest.approx(1.2)


def test_compute_ratios_skips_metric_without_host_sample() -> None:
    agg = {("vm-tcg", "boot_seconds"): 100.0}
    assert bench.compute_ratios(agg) == {}


def test_compute_ratios_skips_zero_host_baseline() -> None:
    agg = {("host", "boot_seconds"): 0.0, ("vm-tcg", "boot_seconds"): 100.0}
    assert bench.compute_ratios(agg) == {}


def test_detect_regressions_flags_only_above_factor() -> None:
    current = {
        ("vm-tcg", "boot_seconds"): 250.0,  # 2.5x over baseline -> regression
        ("host", "boot_seconds"): 11.0,  # 1.1x -> fine
    }
    baseline = {
        ("vm-tcg", "boot_seconds"): 100.0,
        ("host", "boot_seconds"): 10.0,
    }
    regs = bench.detect_regressions(current, baseline, factor=2.0)
    assert len(regs) == 1
    assert regs[0].backend == "vm-tcg"
    assert regs[0].factor == pytest.approx(2.5)


def test_detect_regressions_sorted_by_factor_descending() -> None:
    current = {("a", "m"): 30.0, ("b", "m"): 50.0}
    baseline = {("a", "m"): 10.0, ("b", "m"): 10.0}
    regs = bench.detect_regressions(current, baseline, factor=2.0)
    assert [r.backend for r in regs] == ["b", "a"]


def test_detect_regressions_ignores_missing_and_zero_baseline() -> None:
    current = {("a", "m"): 100.0, ("b", "m"): 100.0}
    baseline = {("b", "m"): 0.0}  # 'a' missing, 'b' zero -> both skipped
    assert bench.detect_regressions(current, baseline) == []


def test_render_markdown_has_tables_and_all_clear() -> None:
    agg = {("host", "boot_seconds"): 10.0, ("vm-tcg", "boot_seconds"): 100.0}
    md = bench.render_markdown(agg, bench.compute_ratios(agg), [])
    assert "## Host-vs-VM benchmark" in md
    assert "Wall-time (seconds)" in md
    assert "10.00x" in md  # vm-tcg ratio
    assert "white_check_mark" in md


def test_render_markdown_lists_regressions() -> None:
    agg = {("host", "boot_seconds"): 10.0, ("vm-tcg", "boot_seconds"): 300.0}
    regs = [
        bench.Regression(
            backend="vm-tcg",
            metric="boot_seconds",
            current=300.0,
            baseline=100.0,
            factor=3.0,
        )
    ]
    md = bench.render_markdown(agg, bench.compute_ratios(agg), regs)
    assert "1 regression(s)" in md
    assert "3.00x" in md
    # baseline column precedes current column in the row.
    row = next(line for line in md.splitlines() if line.startswith("| vm-tcg |"))
    assert row.index("100.0s") < row.index("300.0s")


def test_render_markdown_missing_backend_renders_dash() -> None:
    agg = {("host", "boot_seconds"): 10.0}  # no vm sample at all
    md = bench.render_markdown(agg, bench.compute_ratios(agg), [])
    assert "| boot_seconds | 10.0 |" in md


def test_save_and_load_samples_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "samples.json"
    original = _samples(("host", "boot_seconds", 12.5), ("vm-tcg", "boot_seconds", 99.0))
    bench.save_samples(path, original, runner="ubuntu-latest")
    blob = json.loads(path.read_text())
    assert blob["runner"] == "ubuntu-latest"
    assert bench.load_samples(path) == original


def test_load_samples_accepts_bare_list(tmp_path: Path) -> None:
    path = tmp_path / "bare.json"
    path.write_text(json.dumps([{"backend": "host", "metric": "boot_seconds", "seconds": 1.0}]))
    assert bench.load_samples(path) == _samples(("host", "boot_seconds", 1.0))


def test_load_samples_missing_file_is_empty(tmp_path: Path) -> None:
    assert bench.load_samples(tmp_path / "nope.json") == []


def test_measure_records_sample_for_successful_command(tmp_path: Path) -> None:
    samples_file = tmp_path / "s.json"
    rc = bench.main(
        [
            "measure",
            "--backend",
            "host",
            "--metric",
            "boot_seconds",
            "--samples-file",
            str(samples_file),
            "--runner",
            "test-runner",
            "--",
            "true",
        ]
    )
    assert rc == 0
    recorded = bench.load_samples(samples_file)
    assert len(recorded) == 1
    assert recorded[0].backend == "host"
    assert recorded[0].seconds >= 0.0


def test_measure_appends_to_existing_samples(tmp_path: Path) -> None:
    samples_file = tmp_path / "s.json"
    for backend in ("host", "vm-tcg"):
        bench.main(
            [
                "measure",
                "--backend",
                backend,
                "--metric",
                "boot_seconds",
                "--samples-file",
                str(samples_file),
                "--",
                "true",
            ]
        )
    assert {s.backend for s in bench.load_samples(samples_file)} == {"host", "vm-tcg"}


def test_measure_failed_command_records_nothing(tmp_path: Path) -> None:
    samples_file = tmp_path / "s.json"
    rc = bench.main(
        [
            "measure",
            "--backend",
            "host",
            "--metric",
            "boot_seconds",
            "--samples-file",
            str(samples_file),
            "--",
            "false",
        ]
    )
    assert rc != 0
    assert bench.load_samples(samples_file) == []


def test_record_appends_pre_measured_sample(tmp_path: Path) -> None:
    samples_file = tmp_path / "s.json"
    rc = bench.main(
        [
            "record",
            "--backend",
            "vm-tcg",
            "--metric",
            "boot_seconds",
            "--seconds",
            "98.5",
            "--samples-file",
            str(samples_file),
        ]
    )
    assert rc == 0
    recorded = bench.load_samples(samples_file)
    assert recorded == _samples(("vm-tcg", "boot_seconds", 98.5))


def test_report_writes_summary_and_results(tmp_path: Path) -> None:
    samples_file = tmp_path / "s.json"
    bench.save_samples(
        samples_file,
        _samples(("host", "boot_seconds", 10.0), ("vm-tcg", "boot_seconds", 100.0)),
    )
    summary = tmp_path / "summary.md"
    results = tmp_path / "results.json"
    rc = bench.main(
        [
            "report",
            "--samples-file",
            str(samples_file),
            "--summary",
            str(summary),
            "--results-out",
            str(results),
        ]
    )
    assert rc == 0
    assert "Host-vs-VM benchmark" in summary.read_text()
    parsed = json.loads(results.read_text())
    assert parsed["ratios"]["boot_seconds"]["vm-tcg"] == 10.0


def test_report_alerts_on_regression_but_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    samples_file = tmp_path / "s.json"
    baseline_file = tmp_path / "baseline.json"
    bench.save_samples(samples_file, _samples(("vm-tcg", "boot_seconds", 300.0)))
    bench.save_samples(baseline_file, _samples(("vm-tcg", "boot_seconds", 100.0)))
    rc = bench.main(
        [
            "report",
            "--samples-file",
            str(samples_file),
            "--baseline",
            str(baseline_file),
            "--summary",
            str(tmp_path / "out.md"),
        ]
    )
    assert rc == 0  # track, don't gate
    assert "::warning title=Benchmark regression::" in capsys.readouterr().out


def test_report_uses_github_step_summary_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    samples_file = tmp_path / "s.json"
    step_summary = tmp_path / "step.md"
    bench.save_samples(samples_file, _samples(("host", "boot_seconds", 10.0)))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(step_summary))
    bench.main(["report", "--samples-file", str(samples_file)])
    assert "Host-vs-VM benchmark" in step_summary.read_text()


def test_report_missing_baseline_is_tolerated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    samples_file = tmp_path / "s.json"
    bench.save_samples(samples_file, _samples(("host", "boot_seconds", 10.0)))
    rc = bench.main(
        [
            "report",
            "--samples-file",
            str(samples_file),
            "--baseline",
            str(tmp_path / "absent.json"),
        ]
    )
    assert rc == 0
    assert "not found" in capsys.readouterr().out


def test_report_prints_to_stdout_without_summary_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    samples_file = tmp_path / "s.json"
    bench.save_samples(samples_file, _samples(("host", "boot_seconds", 10.0)))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    bench.main(["report", "--samples-file", str(samples_file)])
    assert "Host-vs-VM benchmark" in capsys.readouterr().out

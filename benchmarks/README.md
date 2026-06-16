# Benchmark baselines

This directory holds the committed performance baseline the nightly
[`benchmark.yml`](../.github/workflows/benchmark.yml) workflow trends new runs
against (issue #50). Benchmarking **tracks, it does not gate** — a regression
only raises a GitHub `::warning::` annotation in the nightly job; it never fails
a PR.

## `baseline.json`

The reference wall-times, in the same schema `scripts/bench.py` reads and
writes:

```json
{ "samples": [ { "backend": "...", "metric": "...", "seconds": 0.0 } ] }
```

* `backend` — `host` (host-binder redroid), `vm-tcg` / `vm-kvm` (the QEMU
  micro-VM backend under TCG / KVM), or `build` (the one-time kernel/rootfs
  compile).
* `metric` — `boot_seconds`, `postboot_seconds`, or `compile_seconds`.

The seeded values come from the offline R&D in
[`docs/design/vm-rnd-log.md`](../docs/design/vm-rnd-log.md) Stage A (pure TCG,
no `/dev/kvm`): a ~450 s kernel build and a ~100 s TCG boot. There is no `host`
baseline yet — the first green nightly captures it.

## Refreshing the baseline

The nightly run uploads a `bench-results` artifact containing the run's
`samples.json`. To re-baseline after an intentional change (a new kernel
config, a QEMU bump), download that artifact and copy its `samples.json` over
`baseline.json` in a reviewed PR. Keep the file small — it is the *reference*,
not a history (the trend lives in the per-run artifacts).

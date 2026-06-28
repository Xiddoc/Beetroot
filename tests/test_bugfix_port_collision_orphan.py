"""A validation-passing-but-index-colliding config must not poison or orphan.

Two tightly-coupled bugs share one root cause: a ``ports:`` list can pass
pydantic validation (``config._check_ports_distinct`` only checks distinctness
among the *explicit* host ports — it has no knowledge of the instance index)
yet still raise ``ports.PortCollisionError`` at resolution time, because an
entry pinned to a sibling's stride default only collides once that sibling's
stride port is computed for the instance's index.

``ports: [{service: adb, guest: 5555}, {service: x, guest: 8080, host: 5555}]``
validates, but at index 0 ``adb`` resolves to its stride default 5555 and ``x``
is pinned to 5555 — a self-collision that ``resolve_ports`` rejects.

* Bug 1 (registry): ``all_resolved_host_ports`` resolved every registered
  instance's ports OUTSIDE the ``FileNotFoundError`` guard, so a single
  poisoned row crashed every *other* instance's cross-instance scan
  (create / register / apply / restore), aborting the unrelated operation with
  a misattributed error. The fix falls back to the instance's well-known stride
  defaults when its resolution raises, instead of crashing the scan.
* Bug 2 (api): ``Instance.create`` / ``Instance.register`` resolved the ports
  AFTER committing the registry row (``add_allocating``) but BEFORE the
  rollback try/except, so a resolution raise escaped before rollback ran,
  orphaning a registry row the user thought had failed. The fix moves the
  resolve INSIDE the rollback try so the orphan is cleaned up.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from beetroot import api, config, paths, ports, registry

# A config that PASSES pydantic validation but whose ports resolve to a
# self-collision at index 0 (adb's stride default 5555 == x's pinned host
# 5555). ``api_version: 8`` is the schema this list shape belongs to.
_POISONED_YAML = (
    "api_version: 8\n"
    "android:\n"
    "  version: 14\n"
    "ports:\n"
    "  - {service: adb, guest: 5555}\n"
    "  - {service: x, guest: 8080, host: 5555}\n"
)


def _poisoned_config() -> config.InstanceConfig:
    """Load the poisoned YAML into a model (proves it validates).

    Loading must succeed; only later port *resolution* raises. If pydantic
    ever started rejecting this shape, this helper would raise here and the
    tests below would no longer exercise the bug — which is exactly the
    regression we want surfaced loudly.
    """
    import tempfile

    p = Path(tempfile.mkdtemp()) / "beetroot.yaml"
    p.write_text(_POISONED_YAML)
    return config.load_yaml(p)


def test_poisoned_yaml_validates_but_resolve_raises() -> None:
    # Guard the test's own premise: the YAML loads (validation passes) but
    # resolving it at index 0 raises PortCollisionError. If this stops
    # holding, the bug tests below are no longer exercising the bug.
    cfg = _poisoned_config()
    with pytest.raises(ports.PortCollisionError):
        ports.resolve_ports(0, cfg.ports)


# ---------------------------------------------------------------------------
# Bug 1 — a poisoned registry row must not crash the cross-instance scan.
# ---------------------------------------------------------------------------


def _register_poisoned_directory(root: Path, name: str) -> None:
    """Stage a directory-backed registry row whose on-disk YAML is poisoned.

    Writes the poisoned ``beetroot.yaml`` into ``root`` and registers a
    redroid backend pointing at it — the lowest-level reproduction of a row
    whose ``ports:`` resolution raises. Because the registry is empty,
    ``add_allocating`` hands it index 0, which is exactly where the config
    self-collides.
    """
    root.mkdir(parents=True, exist_ok=True)
    paths.instance_yaml(root).write_text(_POISONED_YAML)
    index = registry.add_allocating(
        name,
        backend=registry.RedroidBackendConfig(absolute_path=str(root)),
    )
    assert index == 0


def test_all_resolved_host_ports_survives_poisoned_row(isolated_registry: Path) -> None:
    poisoned_root = isolated_registry / "poisoned"
    _register_poisoned_directory(poisoned_root, "poisoned")

    # The scan must NOT raise — the poisoned row falls back to its stride
    # defaults instead of crashing the whole scan.
    resolved = registry.all_resolved_host_ports()

    assert "poisoned" in resolved
    # The fallback is the well-known stride defaults for index 0 — the
    # instance's protected ports still count cross-instance.
    assert resolved["poisoned"] == set(ports.ports_for_index(0).values())


def test_clean_instance_creates_despite_poisoned_sibling(
    cli_root: Path, isolated_registry: Path
) -> None:
    # A poisoned sibling already sits in the registry at index 0.
    poisoned_root = isolated_registry / "poisoned"
    _register_poisoned_directory(poisoned_root, "poisoned")

    # Creating an unrelated clean instance runs a cross-instance scan
    # (all_resolved_host_ports) that iterates the poisoned row. Before the
    # fix, that scan crashed and aborted this unrelated create.
    target = cli_root / "clean"
    inst = api.Instance.create("clean", path=target)

    assert inst.name == "clean"
    assert registry.get("clean") is not None
    # The clean instance landed at the next free index (1) — its adb stride
    # port (5565) does not collide with the poisoned sibling's protected 5555.
    clean_meta = registry.get("clean")
    assert clean_meta is not None
    assert clean_meta.index == 1


# ---------------------------------------------------------------------------
# Bug 2 — a resolution raise must roll back, not orphan, the registry row.
# ---------------------------------------------------------------------------


def test_create_with_poisoned_config_raises_and_leaves_no_orphan(cli_root: Path) -> None:
    cfg = _poisoned_config()
    target = cli_root / "alpha"

    with pytest.raises(ports.PortCollisionError):
        api.Instance.create("alpha", path=target, cfg=cfg)

    # Rollback ran: the just-committed registry row is gone, not orphaned.
    assert registry.get("alpha") is None
    # The freshly-created directory is cleaned up too — no half-staged debris.
    assert not target.exists()


def test_register_with_poisoned_config_raises_and_leaves_no_orphan(cli_root: Path) -> None:
    target = cli_root / "bravo"
    target.mkdir()
    paths.instance_yaml(target).write_text(_POISONED_YAML)

    with pytest.raises(ports.PortCollisionError):
        api.Instance.register(target, name="bravo")

    # Rollback ran: no orphaned registry row.
    assert registry.get("bravo") is None
    # ``register`` adopts a pre-existing dir — it must survive the rollback.
    assert target.exists()
    assert paths.instance_yaml(target).is_file()

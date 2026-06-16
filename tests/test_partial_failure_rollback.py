"""Partial-failure rollback for Instance.create / register / snapshot.restore.

CR #3 finding 4: ``Instance.create``, ``Instance.register``, and
``snapshot.restore`` all write to disk (``mkdir`` + ``write_yaml`` /
``_extract_archive_into``) BEFORE the port-collision check AND
``_stage_local()`` run. If either step raises, the on-disk directory
+ YAML stick around, the registry row was already added by
``add_allocating``, and the user had to manually clean up the debris
before retrying.

The fix wraps ``_check_port_collisions + _stage_local`` in try/except
in each constructor. On failure:

  (a) The registry row is removed (``registry.remove(name)``) — this
      was already done for the port-collision branch, the fix
      extends it to every other constructor exception.
  (b) If Beetroot created the on-disk directory in this call (tracked
      via a local ``created_dir`` flag), ``shutil.rmtree(target_root)``
      cleans it up too.
  (c) Pre-existing directories (e.g. ``register`` adopting a user's
      existing dir) are NEVER deleted on rollback. Destroying a user's
      pre-existing files because a port collided would be a data-loss
      footgun.

T2 split ``_stage`` into ``_stage_local`` (rollback-safe — env
file, dirs, frida placeholder) and ``_stage_network`` (post-commit
soft-fail — Frida download + module fetch). Network errors now
print a hint and leave the instance registered for the user to retry
via ``beetroot apply``; rollback only fires for ``_stage_local``
failures. (T2 Agent 2 B-2.)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from docker_daemon import daemon_available

from beetroot import api, config, modules_download, paths, registry, snapshot

# Restore tests that free the source slot via ``destroy`` → ``compose down``
# need a live Docker daemon; skip (don't fail) when there isn't one (#59).
_needs_daemon = pytest.mark.skipif(not daemon_available(), reason="docker daemon not available")


def _poison_stage_local(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> dict[str, bool]:
    """Replace ``Instance._stage_local`` with a stub that raises ``exc``.

    The local-stage step is the rollback-fatal one. Returns a one-key
    dict whose ``called`` field flips True on the first invocation —
    tests use it to assert the rollback path was actually exercised.
    """
    state = {"called": False}

    def _boom(self: api.Instance) -> None:
        state["called"] = True
        raise exc

    monkeypatch.setattr(api.Instance, "_stage_local", _boom)
    return state


def _poison_stage_network(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> dict[str, bool]:
    """Replace ``Instance._stage_network`` with a stub that raises ``exc``.

    The network-stage step is soft-fail. Tests assert the instance is
    still registered and the directory survives.
    """
    state = {"called": False}

    def _boom(self: api.Instance) -> None:
        state["called"] = True
        raise exc

    monkeypatch.setattr(api.Instance, "_stage_network", _boom)
    return state


# ---------------------------------------------------------------------------
# Instance.create
# ---------------------------------------------------------------------------


class TestCreateRollback:
    def test_local_stage_failure_rolls_back_registry_and_dir(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A local-stage failure (filesystem error, disk full, etc.)
        # MUST roll back the registry row + the freshly-created dir.
        state = _poison_stage_local(
            monkeypatch,
            OSError("disk full while writing .env"),
        )

        target = cli_root / "alpha"
        with pytest.raises(OSError, match="disk full"):
            api.Instance.create("alpha", path=target)

        # Registry row is gone.
        assert registry.get("alpha") is None
        # The directory we created is gone — no half-staged debris.
        assert not target.exists()
        # The poison fired (proves we actually exercised the rollback
        # path, not some earlier short-circuit).
        assert state["called"]

    def test_local_stage_failure_leaves_unrelated_paths_alone(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A sibling instance + its data dir must survive a failed
        # create() somewhere else in the world. This pins that
        # rollback's rmtree is targeted at target_root only.
        sibling = cli_root / "bravo"
        api.Instance.create("bravo", path=sibling)
        marker = paths.instance_data(sibling) / "marker.txt"
        marker.write_text("survives")

        _poison_stage_local(monkeypatch, RuntimeError("oh no"))
        target = cli_root / "alpha"
        with pytest.raises(RuntimeError):
            api.Instance.create("alpha", path=target)

        assert sibling.exists()
        assert marker.read_text() == "survives"
        assert registry.get("bravo") is not None

    def test_create_preserves_preexisting_dir_on_rollback(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The CLI's ``--from-data`` flow ``mkdir``s the target then
        # ``copytree``s an external data dir into it before calling
        # ``Instance.create(name, path=target_root)``. If create then
        # fails, those copied bytes must survive — rmtree-on-rollback
        # would destroy the user's freshly-copied data.
        target = cli_root / "alpha"
        target.mkdir()
        (target / "preexisting.txt").write_text("user owns me")

        _poison_stage_local(monkeypatch, RuntimeError("staging blew up"))
        with pytest.raises(RuntimeError):
            api.Instance.create("alpha", path=target)

        # Registry row is gone (we removed it).
        assert registry.get("alpha") is None
        # But the directory + the user's pre-existing file survive.
        assert target.exists()
        assert (target / "preexisting.txt").read_text() == "user owns me"

    def test_network_stage_failure_does_not_roll_back(
        self,
        cli_root: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # T2 Agent 2 B-2: network-stage failures (Frida 404, module
        # HTTP error) are soft. The instance MUST remain registered
        # so the user can re-run ``beetroot apply`` once the network
        # heals — a hard rollback for a transient outage was too
        # aggressive.
        state = _poison_stage_network(
            monkeypatch,
            modules_download.ModuleFetchError("download failed: HTTP 404"),
        )
        target = cli_root / "alpha"
        # ``create`` must NOT raise — the soft-fail path swallows.
        inst = api.Instance.create("alpha", path=target)
        assert state["called"]
        # Registry row + directory survive.
        assert registry.get("alpha") is not None
        assert target.exists()
        assert paths.instance_env(target).is_file()
        # User-visible hint mentions the instance + the apply nudge.
        captured = capsys.readouterr()
        assert "alpha" in captured.err
        assert "beetroot apply" in captured.err
        # Returned instance is usable for the retry.
        assert inst.name == "alpha"


# ---------------------------------------------------------------------------
# Instance.register
# ---------------------------------------------------------------------------


class TestRegisterRollback:
    def test_register_local_stage_failure_leaves_dir_intact(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``register`` adopts a user-owned directory. The rollback path
        # MUST NOT rmtree it on failure — the user's beetroot.yaml +
        # data/ + any other files must survive.
        target = cli_root / "external"
        target.mkdir()
        config.write_yaml(target / "beetroot.yaml", config.InstanceConfig())
        marker = target / "extra.txt"
        marker.write_text("user owns me")

        _poison_stage_local(monkeypatch, RuntimeError("nope"))
        with pytest.raises(RuntimeError):
            api.Instance.register(target)

        assert registry.get("external") is None
        assert target.exists()
        # The user's beetroot.yaml + extra file are untouched.
        assert (target / "beetroot.yaml").is_file()
        assert marker.read_text() == "user owns me"

    def test_register_network_stage_failure_does_not_roll_back(
        self,
        cli_root: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # T2 Agent 2 B-2: same soft-fail contract as create's network
        # branch — register() returns an instance and the registry
        # row stays.
        target = cli_root / "external"
        target.mkdir()
        config.write_yaml(target / "beetroot.yaml", config.InstanceConfig())

        _poison_stage_network(
            monkeypatch,
            modules_download.ModuleFetchError("HTTP 404"),
        )
        inst = api.Instance.register(target)
        assert registry.get("external") is not None
        assert inst.name == "external"
        captured = capsys.readouterr()
        assert "beetroot apply external" in captured.err


# ---------------------------------------------------------------------------
# snapshot.restore
# ---------------------------------------------------------------------------


class TestRestoreRollback:
    @_needs_daemon
    def test_restore_local_stage_failure_rolls_back_registry_and_dir(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Build a valid archive first so the manifest-read passes.
        src = cli_root / "alpha"
        api.Instance.create("alpha", path=src)
        archive = snapshot.snapshot(src, cli_root / "alpha.tar.zst")
        api.Instance.load("alpha").destroy(yes=True)

        # Poison _stage_local so the restore fails AFTER the registry
        # write + archive extraction. Rollback must remove both the
        # registry row AND the freshly-extracted directory.
        _poison_stage_local(monkeypatch, OSError("disk full"))
        target = cli_root / "alpha-restored"

        with pytest.raises(OSError, match="disk full"):
            snapshot.restore(
                archive,
                dest_name="alpha-restored",
                dest_path=target,
            )

        assert registry.get("alpha-restored") is None
        # The freshly-extracted dir is gone (we created it just for
        # this restore — no user data to preserve).
        assert not target.exists()

    @_needs_daemon
    def test_restore_network_stage_failure_does_not_roll_back(
        self,
        cli_root: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # T2 Agent 2 B-2: network failures during restore's stage step
        # are soft-fail too. The restored instance must remain in the
        # registry so the user can run ``beetroot apply``.
        src = cli_root / "alpha"
        api.Instance.create("alpha", path=src)
        archive = snapshot.snapshot(src, cli_root / "alpha.tar.zst")
        api.Instance.load("alpha").destroy(yes=True)

        _poison_stage_network(
            monkeypatch,
            modules_download.ModuleFetchError("HTTP 404"),
        )
        target = cli_root / "alpha-restored"
        snapshot.restore(
            archive,
            dest_name="alpha-restored",
            dest_path=target,
        )
        assert registry.get("alpha-restored") is not None
        assert target.exists()
        captured = capsys.readouterr()
        assert "beetroot apply alpha-restored" in captured.err

    @_needs_daemon
    def test_restore_preserves_preexisting_empty_dir_on_rollback(
        self, cli_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The user may pre-create an empty ``--path`` dir (e.g. mkdir
        # then chmod for permissions). If restore then fails, we
        # remove the registry row but leave the dir alone — the
        # user owns it, not us.
        src = cli_root / "alpha"
        api.Instance.create("alpha", path=src)
        archive = snapshot.snapshot(src, cli_root / "alpha.tar.zst")
        api.Instance.load("alpha").destroy(yes=True)

        target = cli_root / "preexisting-empty"
        target.mkdir()  # User created it.
        _poison_stage_local(monkeypatch, RuntimeError("nope"))
        with pytest.raises(RuntimeError):
            snapshot.restore(
                archive,
                dest_name="alpha-restored",
                dest_path=target,
            )
        assert registry.get("alpha-restored") is None
        # Dir survives — we didn't create it, so we don't delete it.
        assert target.exists()

    def test_restore_port_collision_rolls_back_registry_and_dir(self, cli_root: Path) -> None:
        # Build an archive whose YAML pins ports.adb: 5555. Restore
        # against an existing instance also using 5555 → port
        # collision → rollback. The rolled-back state must have no
        # registry entry AND no leftover directory.
        src = cli_root / "src"
        cfg = config.InstanceConfig(ports=config.Ports(adb=5555))
        api.Instance.create("src", path=src, cfg=cfg)
        archive = snapshot.snapshot(src, cli_root / "src.tar.zst")

        target = cli_root / "restored"
        with pytest.raises(snapshot.SnapshotError, match="collides"):
            snapshot.restore(
                archive,
                dest_name="restored",
                dest_path=target,
            )
        assert registry.get("restored") is None
        assert not target.exists()

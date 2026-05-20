"""Orphan registry entries don't crash `beetroot ls` and can be destroyed.

CR #1 finding 3: ``Manager.list()`` used to call ``Instance.load(name)``
for every registered name, which calls ``config.load_yaml(...)``.
If a v0.2 user manually ``rm -rf``'d an instance directory without
``beetroot destroy``, the registry entry was stale and the file was
gone. ``cli.main()`` only caught a small set of domain exceptions,
so the bare ``FileNotFoundError`` propagated as a Rich-rendered
traceback.

The fix:

1. ``Manager.list()`` silently skips orphan entries.
2. ``Manager.list_orphans()`` is the cleanup-discovery accessor.
3. The CLI ``ls`` verb appends a trailing skip-line if any orphans exist.
4. ``cli.main()`` also catches bare ``FileNotFoundError`` as a belt-and-
   suspenders fallback for verbs that target an orphan by name.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beetroot import api, cli, registry

runner = CliRunner()


class TestManagerListSkipsOrphans:
    def test_orphan_excluded_from_list(self, cli_root: Path) -> None:
        api.Instance.create("alpha")
        api.Instance.create("bravo")
        shutil.rmtree(registry.instance_path("alpha"))
        names = [inst.name for inst in api.Manager.list_instances()]
        assert names == ["bravo"]

    def test_orphan_surfaced_via_list_orphans(self, cli_root: Path) -> None:
        api.Instance.create("alpha")
        api.Instance.create("bravo")
        shutil.rmtree(registry.instance_path("alpha"))
        assert api.Manager.list_orphans() == ["alpha"]

    def test_no_orphans_returns_empty_list(self, cli_root: Path) -> None:
        api.Instance.create("alpha")
        assert api.Manager.list_orphans() == []

    def test_multiple_orphans_returned_sorted(self, cli_root: Path) -> None:
        api.Instance.create("alpha")
        api.Instance.create("bravo")
        api.Instance.create("charlie")
        shutil.rmtree(registry.instance_path("charlie"))
        shutil.rmtree(registry.instance_path("alpha"))
        assert api.Manager.list_orphans() == ["alpha", "charlie"]

    def test_unparseable_yaml_treated_as_orphan(
        self, cli_root: Path,
    ) -> None:
        # T2 (v0.3.1 deferred): a beetroot.yaml that can't be parsed
        # (corrupted bytes, api_version mismatch, hand-edited junk)
        # used to be invisible to both ``list`` and ``list_orphans``.
        # The user had no way to surface it for cleanup. v0.4 treats
        # parse failures as orphans alongside missing-directory rows.
        api.Instance.create("alpha")
        api.Instance.create("bravo")
        # Corrupt alpha's beetroot.yaml so load_yaml raises.
        from beetroot import paths
        paths.instance_yaml(registry.instance_path("alpha")).write_text(
            "this: is: not: valid: yaml: at: all: }}}}\n"
        )
        # ``Manager.list`` skips it (would have crashed otherwise).
        names = [inst.name for inst in api.Manager.list_instances()]
        assert names == ["bravo"]
        # ``list_orphans`` surfaces it for cleanup.
        assert "alpha" in api.Manager.list_orphans()

    def test_api_version_mismatch_treated_as_orphan(
        self, cli_root: Path,
    ) -> None:
        # Same orphan-surfacing contract for a beetroot.yaml that
        # parses as YAML but fails pydantic validation (e.g. an
        # api_version we don't support any more).
        api.Instance.create("alpha")
        from beetroot import paths
        # Future api_version that pydantic will reject.
        paths.instance_yaml(registry.instance_path("alpha")).write_text(
            "api_version: 999\nandroid:\n  version: 14\n"
        )
        assert "alpha" in api.Manager.list_orphans()


class TestCliLsSurfacesOrphans:
    def test_ls_exits_zero_when_only_orphans(self, cli_root: Path) -> None:
        # An instance whose on-disk dir was rm -rf'd behind the CLI's
        # back leaves a stale registry entry. ``beetroot ls`` must
        # exit 0 and surface the orphan, not crash.
        result = runner.invoke(cli.app, ["create", "alpha"])
        assert result.exit_code == 0, result.stderr
        shutil.rmtree(registry.instance_path("alpha"))

        result = runner.invoke(cli.app, ["ls"])
        assert result.exit_code == 0, result.stderr
        assert "skipping 1 orphan" in result.stdout
        assert "alpha" in result.stdout
        assert "beetroot destroy" in result.stdout

    def test_ls_with_mix_shows_both_table_and_orphan_line(
        self, cli_root: Path
    ) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        runner.invoke(cli.app, ["create", "bravo"])
        shutil.rmtree(registry.instance_path("alpha"))

        result = runner.invoke(cli.app, ["ls"])
        assert result.exit_code == 0, result.stderr
        assert "bravo" in result.stdout
        # Header is only printed once.
        assert result.stdout.count("NAME") == 1
        assert "skipping 1 orphan entry" in result.stdout
        assert "alpha" in result.stdout

    def test_ls_multiple_orphans_uses_plural(self, cli_root: Path) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        runner.invoke(cli.app, ["create", "bravo"])
        runner.invoke(cli.app, ["create", "charlie"])
        shutil.rmtree(registry.instance_path("alpha"))
        shutil.rmtree(registry.instance_path("bravo"))

        result = runner.invoke(cli.app, ["ls"])
        assert result.exit_code == 0, result.stderr
        assert "skipping 2 orphan entries" in result.stdout
        assert "alpha" in result.stdout
        assert "bravo" in result.stdout

    def test_ls_json_includes_orphan_skip_line_on_stdout(
        self, cli_root: Path
    ) -> None:
        runner.invoke(cli.app, ["create", "alpha"])
        runner.invoke(cli.app, ["create", "bravo"])
        shutil.rmtree(registry.instance_path("alpha"))

        result = runner.invoke(cli.app, ["ls", "--json"])
        assert result.exit_code == 0, result.stderr
        # JSON has only bravo; orphan-skip line follows.
        assert '"bravo"' in result.stdout
        assert '"alpha"' not in result.stdout
        assert "skipping 1 orphan" in result.stdout


class TestDestroyOrphan:
    def test_destroy_with_yes_cleans_registry_entry(self, cli_root: Path) -> None:
        # `beetroot destroy <orphan> -y` succeeds and removes the
        # registry entry. The on-disk dir is already gone, but the
        # registry row must come out so the name is free again.
        runner.invoke(cli.app, ["create", "alpha"])
        shutil.rmtree(registry.instance_path("alpha"))
        assert registry.get("alpha") is not None

        result = runner.invoke(cli.app, ["destroy", "alpha", "-y"])
        assert result.exit_code == 0, result.stderr
        assert registry.get("alpha") is None
        # Orphan is no longer reported either.
        assert api.Manager.list_orphans() == []


class TestCliMainCatchesFileNotFound:
    def test_cli_main_converts_bare_file_not_found_to_error_line(
        self,
        cli_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def _raise() -> None:
            raise FileNotFoundError(2, "No such file or directory", "/gone")

        monkeypatch.setattr(cli, "app", _raise)
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "error:" in err


class TestOrphanDoesNotBlockOtherVerbs:
    """The final-CR finding: an orphan in the registry was cascading
    through ``registry.all_resolved_ports`` → ``_check_port_collisions``
    and confusing every other verb (``create``, ``register``, ``apply``,
    ``restore``) with a ``FileNotFoundError`` pointing at the orphan's
    YAML instead of the YAML the user was operating on.
    """

    def test_create_succeeds_when_orphan_in_registry(self, cli_root: Path) -> None:
        # Set up an orphan: register alpha, then wipe its dir on disk.
        api.Instance.create("alpha")
        shutil.rmtree(registry.instance_path("alpha"))
        # Creating bravo must succeed — the orphan's missing yaml must
        # not be loaded by the port-collision check.
        result = runner.invoke(cli.app, ["create", "bravo"])
        assert result.exit_code == 0, result.stderr
        assert registry.get("bravo") is not None

    def test_apply_succeeds_when_orphan_in_registry(self, cli_root: Path) -> None:
        api.Instance.create("alpha")
        api.Instance.create("bravo")
        shutil.rmtree(registry.instance_path("alpha"))
        # Apply on the healthy bravo must not surface alpha's missing yaml.
        result = runner.invoke(cli.app, ["apply", "bravo"])
        assert result.exit_code == 0, result.stderr

    def test_all_resolved_ports_skips_orphans(self, cli_root: Path) -> None:
        api.Instance.create("alpha")
        api.Instance.create("bravo")
        shutil.rmtree(registry.instance_path("alpha"))
        resolved = registry.all_resolved_ports()
        assert "alpha" not in resolved
        assert "bravo" in resolved

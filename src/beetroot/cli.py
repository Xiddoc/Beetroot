"""
beetroot — multi-instance Magisk-Android research lab CLI.

The Typer commands here are thin shells over the OOP surface in
:mod:`beetroot.api`. Each verb constructs an :class:`api.Instance` or
calls an :class:`api.Manager` staticmethod; CLI-specific concerns
(stdout formatting, ``error: ...`` lines, ``typer.Exit(1)``) live in
this module, while the lifecycle logic lives behind the OOP layer.

The verbs stay as module-level Typer commands (not bound methods)
because ``@app.command()`` captures the function reference at import
time — wrapping them in a class would break Typer's dispatch.
"""
from __future__ import annotations

import json
import shutil
import sys
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer

from . import api, builder, compose, paths, ports, registry
from . import snapshot as snapshot_mod


class _GappsVariant(str, Enum):
    """GMS variants accepted by ``beetroot build``."""

    none = "none"
    lite = "lite"
    full = "full"
    mindthegapps = "mindthegapps"


app = typer.Typer(
    no_args_is_help=True,
    help="Beetroot — multi-instance Magisk-Android research lab CLI.",
    add_completion=True,
)


def _error(message: str) -> typer.Exit:
    """
    Print ``error: <message>`` to stderr and return a ``typer.Exit(1)``.

    Callers ``raise _error(...)`` so mypy keeps narrowing on the no-return
    branch and so the ``error: ...`` line lands on stderr (matching the
    old ``sys.exit("error: ...")`` behavior, which wrote to stderr).
    """
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code=1)


def _ensure_exists(name: str) -> None:
    if registry.get(name) is None:
        raise _error(f"no instance named {name!r}. Try `beetroot ls`.")


def _load(name: str) -> api.Instance:
    """Load an instance for a verb that has already passed ``_ensure_exists``."""
    return api.Instance.load(name)


def _resolve_names(names: list[str], all_flag: bool) -> list[str]:
    """
    Return the list of instance names from ``--all`` or positional names.

    Raises ``typer.Exit`` on conflicting or missing arguments. The
    ``all_flag`` + empty-registry path exits with code 0 (informational
    "(no instances)" line on stdout), matching the v0.2 argparse behavior.
    """
    if all_flag and names:
        raise _error("--all and explicit names are mutually exclusive.")
    if all_flag:
        instances = registry.list_instances()
        if not instances:
            typer.echo("(no instances)")
            raise typer.Exit(code=0)
        return sorted(instances)
    if not names:
        raise _error("provide at least one instance name, or use --all.")
    return list(names)


# ---- verbs -----------------------------------------------------------------


@app.command()
def create(
    name: Annotated[str, typer.Argument(help="Instance name to register.")],
    path: Annotated[
        Path | None,
        typer.Option("--path", help="Instance directory location (default: ./<name>)."),
    ] = None,
    from_data: Annotated[
        Path | None,
        typer.Option("--from-data", help="Copy an existing data dir as the instance's /data."),
    ] = None,
    preset: Annotated[
        str | None,
        typer.Option(
            "--preset",
            hidden=True,
            help="(removed in v0.3 — see CHANGELOG.md)",
        ),
    ] = None,
) -> None:
    """
    Create a new instance directory and stage its files.

    The new ``beetroot.yaml`` is the minimal valid config (``api_version``
    plus ``android.version``); every other field falls back to schema
    defaults. To start from a richer baseline, copy a file from the
    repo's ``examples/`` directory over the generated ``beetroot.yaml``.
    """
    if preset is not None:
        raise _error(
            f"--preset was removed in v0.3 — copy examples/{preset}.yaml over "
            f"your fresh beetroot.yaml and run 'beetroot apply {name}'."
        )
    if registry.get(name) is not None:
        raise _error(f"instance {name!r} already exists.")

    target_root = Path(path if path is not None else name).resolve()
    if (target_root / "beetroot.yaml").exists():
        raise _error(
            f"{target_root}/beetroot.yaml already exists — "
            f"use `beetroot register {target_root}` to adopt it."
        )

    if from_data is not None:
        src = Path(from_data).resolve()
        if not src.is_dir():
            raise _error(f"--from-data path {src} is not a directory.")
        target_root.mkdir(parents=True, exist_ok=True)
        dst = paths.instance_data(target_root)
        if dst.exists():
            shutil.rmtree(dst)
        typer.echo(f"[beetroot] copying {src} → {dst}")
        shutil.copytree(src, dst)

    try:
        inst = api.Instance.create(name, path=target_root)
    except ValueError as e:
        raise _error(str(e)) from e
    p = inst.ports
    typer.echo(
        f"[beetroot] created {inst.name} at {inst.root} "
        f"(index {inst.index}, ADB localhost:{p['adb']}, Frida localhost:{p['frida']})"
    )
    typer.echo(f"[beetroot] next: beetroot up {inst.name}")


@app.command()
def register(
    path: Annotated[Path, typer.Argument(help="Path to a directory containing beetroot.yaml.")],
    name: Annotated[
        str | None,
        typer.Option("--name", help="Registry name (default: basename of path)."),
    ] = None,
) -> None:
    """Adopt an existing instance directory under the global registry."""
    target_root = path.resolve()
    resolved_name = name if name is not None else target_root.name
    if registry.get(resolved_name) is not None:
        raise _error(
            f"instance {resolved_name!r} already registered. Use a different "
            f"--name, or `beetroot destroy {resolved_name}` first."
        )
    try:
        inst = api.Instance.register(path, name=name)
    except FileNotFoundError as e:
        raise _error(str(e)) from e
    except ValueError as e:
        raise _error(str(e)) from e
    p = inst.ports
    typer.echo(
        f"[beetroot] registered {inst.name} at {inst.root} "
        f"(index {inst.index}, ADB localhost:{p['adb']}, "
        f"Frida localhost:{p['frida']})"
    )


@app.command()
def apply(
    name: Annotated[str, typer.Argument(help="Instance name.")],
) -> None:
    """Re-render ``.env`` and re-stage files from the instance's beetroot.yaml."""
    _ensure_exists(name)
    inst = _load(name)
    try:
        inst.apply()
    except ValueError as e:
        raise _error(str(e)) from e
    typer.echo(f"[beetroot] re-staged {name} from beetroot.yaml")
    typer.echo(f"[beetroot] restart with: beetroot down {name} && beetroot up {name}")


@app.command()
def up(
    names: Annotated[
        list[str] | None,
        typer.Argument(help="Instance names to start."),
    ] = None,
    all_: Annotated[
        bool,
        typer.Option("--all", help="Act on all registered instances."),
    ] = False,
) -> None:
    """Start one or more instances."""
    for instance_name in _resolve_names(list(names or []), all_):
        _ensure_exists(instance_name)
        inst = _load(instance_name)
        inst.up()
        p = inst.ports
        typer.echo(
            f"[beetroot] {inst.name} up — "
            f"ADB localhost:{p['adb']}, Frida localhost:{p['frida']}"
        )


@app.command()
def down(
    names: Annotated[
        list[str] | None,
        typer.Argument(help="Instance names to stop."),
    ] = None,
    all_: Annotated[
        bool,
        typer.Option("--all", help="Act on all registered instances."),
    ] = False,
) -> None:
    """Stop one or more instances, preserving data."""
    for instance_name in _resolve_names(list(names or []), all_):
        _ensure_exists(instance_name)
        _load(instance_name).down()
        typer.echo(f"[beetroot] {instance_name} down (data preserved)")


@app.command()
def restart(
    names: Annotated[
        list[str] | None,
        typer.Argument(help="Instance names to restart."),
    ] = None,
    all_: Annotated[
        bool,
        typer.Option("--all", help="Act on all registered instances."),
    ] = False,
) -> None:
    """Stop then start one or more instances."""
    for instance_name in _resolve_names(list(names or []), all_):
        _ensure_exists(instance_name)
        _load(instance_name).restart()
        typer.echo(f"[beetroot] {instance_name} restarted")


@app.command()
def destroy(
    name: Annotated[str, typer.Argument(help="Instance name to destroy.")],
    yes: Annotated[
        bool,
        typer.Option("-y", "--yes", help="Skip confirmation."),
    ] = False,
) -> None:
    """Stop and permanently delete an instance including its data directory."""
    _ensure_exists(name)
    root = registry.instance_path(name)
    if not yes:
        ans = input(f"Destroy {name} and delete {root}? [y/N] ").strip().lower()
        if ans != "y":
            typer.echo("[beetroot] aborted")
            return
    try:
        compose.down(name, root, volumes=True)
    except compose.ComposeError as e:
        # Surface the compose failure as a "continuing" advisory so the
        # user knows the host-side cleanup still ran.
        typer.echo(f"[beetroot] (compose down failed: {e}; continuing)")
    if root.exists():
        shutil.rmtree(root)
    registry.remove(name)
    typer.echo(f"[beetroot] destroyed {name}")


@app.command(name="ls")
def ls(
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON instead of the human-readable table."),
    ] = False,
) -> None:
    """List all registered instances with their ports and live status."""
    instances = api.Manager.list()
    if json_out:
        out = {}
        for inst in instances:
            p = inst.ports
            meta = registry.get(inst.name)
            assert meta is not None
            out[inst.name] = {
                "path": str(inst.root),
                "index": inst.index,
                "adb": f"localhost:{p['adb']}",
                "frida": f"localhost:{p['frida']}",
                "status": inst.status,
                "created_at": meta["created_at"],
            }
        typer.echo(json.dumps(out, indent=2, sort_keys=True))
        return

    if not instances:
        typer.echo("(no instances — try `beetroot create alpha`)")
        return
    typer.echo(f"{'NAME':<14}{'IDX':<5}{'ADB':<22}{'FRIDA':<22}{'STATUS':<14}{'PATH'}")
    for inst in instances:
        p = inst.ports
        typer.echo(
            f"{inst.name:<14}{inst.index:<5}"
            f"{'localhost:' + str(p['adb']):<22}"
            f"{'localhost:' + str(p['frida']):<22}"
            f"{inst.status:<14}"
            f"{inst.root}"
        )


@app.command()
def logs(
    name: Annotated[str, typer.Argument(help="Instance name.")],
    follow: Annotated[
        bool,
        typer.Option("-f", "--follow", help="Follow log output."),
    ] = False,
) -> None:
    """Tail container logs for an instance."""
    _ensure_exists(name)
    _load(name).logs(follow=follow)


@app.command()
def shell(
    name: Annotated[str, typer.Argument(help="Instance name.")],
) -> None:
    """Open an interactive ADB shell into an instance."""
    _ensure_exists(name)
    try:
        _load(name).shell()
    except api.AdbNotInstalledError as e:
        raise _error(str(e)) from e


@app.command()
def env(
    name: Annotated[str, typer.Argument(help="Instance name.")],
) -> None:
    """Print eval-able ``ANDROID_DEVICE`` / ``FRIDA_DEVICE`` shell exports."""
    _ensure_exists(name)
    inst = _load(name)
    typer.echo(f"export ANDROID_DEVICE={inst.adb_address}")
    typer.echo(f"export FRIDA_DEVICE={inst.frida_address}")


@app.command(
    name="frida",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def frida(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Instance name.")],
) -> None:
    """
    Invoke the frida CLI against an instance, forwarding extra arguments.

    Any tokens after ``<name>`` (e.g. ``-n com.app``, ``-f com.app -l script.js``)
    are passed verbatim to the underlying ``frida`` CLI, after Beetroot prepends
    ``-H localhost:<frida_port>``. Use ``--`` to disambiguate frida flags from
    Typer's own option-parsing if needed.
    """
    _ensure_exists(name)
    try:
        _load(name).frida_cli(list(ctx.args))
    except api.FridaNotInstalledError as e:
        raise _error(str(e)) from e


@app.command()
def module(
    name: Annotated[str, typer.Argument(help="Instance name.")],
    source: Annotated[
        str,
        typer.Argument(help="https URL or instance-relative path to a .zip."),
    ],
    sha256: Annotated[
        str | None,
        typer.Option("--sha256", metavar="HEX", help="Expected sha256 hex digest of the zip."),
    ] = None,
) -> None:
    """Append a module to beetroot.yaml and re-stage. Caller restarts."""
    _ensure_exists(name)
    inst = _load(name)
    inst.add_module(source, sha256=sha256)
    typer.echo(f"[beetroot] added module → {paths.instance_yaml(inst.root)}")
    typer.echo(f"[beetroot] restart to flash: beetroot down {name} && beetroot up {name}")


@app.command(name="setup", hidden=True)
def setup_deprecated(
    args: Annotated[
        list[str] | None,
        typer.Argument(help="(removed in v0.3)"),
    ] = None,
) -> None:
    """(Deprecated alias for `beetroot build` — see CHANGELOG.md.)"""
    # `args` is declared so v0.2 invocations like `beetroot setup lite`
    # still match this verb (Typer would otherwise reject the trailing
    # positional). The value itself is ignored.
    del args
    raise _error(
        "the 'setup' verb was renamed to 'build' in v0.3 — "
        "run 'beetroot build [variant]' (see CHANGELOG.md)."
    )


@app.command()
def build(
    gapps: Annotated[
        _GappsVariant,
        typer.Argument(help="GMS variant to bake into the base image."),
    ] = _GappsVariant.lite,
) -> None:
    """Build the redroid base image and Beetroot layer for a gapps variant."""
    tag = builder.build_image(gapps=gapps.value)
    typer.echo(f"[beetroot] base image built: {tag}")


@app.command(name="snapshot")
def snapshot(
    name: Annotated[str, typer.Argument(help="Instance name to snapshot.")],
    output: Annotated[
        Path | None,
        typer.Option("-o", "--output", help="Archive path (default: ./<name>.tar.zst)."),
    ] = None,
) -> None:
    """Pack an instance's host-side state into a ``.tar.zst`` archive."""
    _ensure_exists(name)
    inst = _load(name)
    dest = output if output is not None else Path(f"{name}.tar.zst")
    try:
        final = inst.snapshot(dest)
    except snapshot_mod.SnapshotError as e:
        raise _error(str(e)) from e
    typer.echo(f"[beetroot] snapshot of {name} → {final}")


@app.command(name="restore")
def restore(
    archive: Annotated[
        Path,
        typer.Argument(help="Path to a .tar.zst snapshot archive."),
    ],
    as_: Annotated[
        str | None,
        typer.Option("--as", help="Registry name for the restored instance."),
    ] = None,
    path: Annotated[
        Path | None,
        typer.Option("--path", help="Directory to restore into (default: ./<name>)."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite the destination directory if non-empty."),
    ] = False,
) -> None:
    """Unpack a snapshot archive into a new instance and register it."""
    try:
        manifest = snapshot_mod.read_manifest(archive)
    except snapshot_mod.SnapshotError as e:
        raise _error(str(e)) from e
    dest_name = as_ if as_ is not None else manifest.name
    dest_path = (path if path is not None else Path(dest_name)).resolve()
    try:
        restored = snapshot_mod.restore(
            archive, dest_name=dest_name, dest_path=dest_path, force=force,
        )
    except snapshot_mod.SnapshotError as e:
        raise _error(str(e)) from e
    inst = _load(dest_name)
    p = inst.ports
    typer.echo(
        f"[beetroot] restored {dest_name} at {restored} "
        f"(index {inst.index}, ADB localhost:{p['adb']}, Frida localhost:{p['frida']})"
    )
    typer.echo(f"[beetroot] next: beetroot apply {dest_name} && beetroot up {dest_name}")


def main() -> None:
    """
    Parse CLI arguments and dispatch to the appropriate command handler.

    Wraps ``app()`` to convert domain exceptions raised from deep in the
    procedural call tree into the same friendly ``error: ...`` line +
    ``exit 1`` shape the rest of the CLI uses. ``compose.ComposeError``
    and ``builder.BootstrapError`` are caught here because the
    up/down/restart/logs/apply/build verbs let them propagate as plain
    tracebacks otherwise — v0.2 was uniformly ``error: ...``.
    """
    try:
        app()
    except paths.InstanceRootNotFoundError as e:
        typer.echo(f"error: {e}", err=True)
        sys.exit(1)
    except ports.PortCollisionError as e:
        typer.echo(f"error: {e}", err=True)
        sys.exit(1)
    except compose.ComposeError as e:
        typer.echo(f"error: {e}", err=True)
        sys.exit(1)
    except builder.BootstrapError as e:
        typer.echo(f"error: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

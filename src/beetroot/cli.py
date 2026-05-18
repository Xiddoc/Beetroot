"""beetroot — multi-instance Magisk-Android research lab CLI."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer

from . import (
    compose,
    config,
    frida_dl,
    modules_dl,
    paths,
    ports,
    registry,
    setup_runner,
    snapshot as snapshot_mod,
)

_MINIMAL_BEETROOT_YAML = "api_version: 2\nandroid:\n  version: 14\n"


class _GappsVariant(str, Enum):
    """GMS variants accepted by ``beetroot setup``."""

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


def _instance_root(name: str) -> Path:
    """Return the absolute instance directory for a registered ``name``."""
    return registry.instance_path(name)


def _check_port_collisions(name: str, new_ports: dict[str, int]) -> None:
    """Exit with a clear message if ``new_ports`` collide with any other instance."""
    others = {n: p for n, p in registry.all_resolved_ports().items() if n != name}
    collision = registry.find_port_collision(new_ports, others)
    if collision is None:
        return
    port, other_name, kind = collision
    raise _error(
        f"port {port} ({kind}) collides with instance {other_name!r} "
        f"(which also uses {port}). Pin or remove one."
    )


def _stage_instance(name: str, root: Path, cfg: config.InstanceConfig) -> None:
    """Render .env and stage Frida + modules. Idempotent — safe on ``apply``."""
    meta = registry.get(name)
    assert meta is not None
    rendered_ports = ports.resolve_ports(meta["index"], cfg.ports)

    paths.instance_data(root).mkdir(parents=True, exist_ok=True)
    paths.instance_modules(root).mkdir(parents=True, exist_ok=True)

    paths.instance_env(root).write_text(
        config.render_env(name, cfg, rendered_ports)
    )

    if cfg.frida is not None:
        frida_dl.stage_for_instance(root, cfg.frida.version)
    else:
        frida_dl.stage_empty(root)

    modules_dl.stage_for_instance(root, cfg)


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
) -> None:
    """
    Create a new instance directory and stage its files.

    The new ``beetroot.yaml`` is the minimal valid config (``api_version``
    plus ``android.version``); every other field falls back to schema
    defaults. To start from a richer baseline, copy a file from the
    repo's ``examples/`` directory over the generated ``beetroot.yaml``.
    """
    if registry.get(name) is not None:
        raise _error(f"instance {name!r} already exists.")

    target_root = Path(path if path is not None else name).resolve()
    if (target_root / "beetroot.yaml").exists():
        raise _error(
            f"{target_root}/beetroot.yaml already exists — "
            f"use `beetroot register {target_root}` to adopt it."
        )

    cfg = config.InstanceConfig()
    index = ports.lowest_free_index(registry.used_indices())
    new_ports = ports.resolve_ports(index, cfg.ports)
    _check_port_collisions(name, new_ports)

    target_root.mkdir(parents=True, exist_ok=True)
    paths.instance_yaml(target_root).write_text(_MINIMAL_BEETROOT_YAML)

    registry.add(name, target_root, index)

    if from_data is not None:
        src = Path(from_data).resolve()
        if not src.is_dir():
            raise _error(f"--from-data path {src} is not a directory.")
        dst = paths.instance_data(target_root)
        if dst.exists():
            shutil.rmtree(dst)
        typer.echo(f"[beetroot] copying {src} → {dst}")
        shutil.copytree(src, dst)

    _stage_instance(name, target_root, cfg)
    p = ports.resolve_ports(index, cfg.ports)
    typer.echo(
        f"[beetroot] created {name} at {target_root} "
        f"(index {index}, ADB localhost:{p['adb']}, Frida localhost:{p['frida']})"
    )
    typer.echo(f"[beetroot] next: beetroot up {name}")


@app.command()
def register(
    path: Annotated[Path, typer.Argument(help="Path to a directory containing beetroot.yaml.")],
    name: Annotated[
        str | None,
        typer.Option("--name", help="Registry name (default: basename of path)."),
    ] = None,
) -> None:
    """Adopt an existing instance directory under the global registry."""
    target_root = Path(path).resolve()
    yaml_path = paths.instance_yaml(target_root)
    if not yaml_path.is_file():
        raise _error(f"no beetroot.yaml at {yaml_path}.")
    resolved_name = name or target_root.name
    if registry.get(resolved_name) is not None:
        raise _error(
            f"instance {resolved_name!r} already registered. Use a different "
            f"--name, or `beetroot destroy {resolved_name}` first."
        )
    cfg = config.load_yaml(yaml_path)
    index = ports.lowest_free_index(registry.used_indices())
    new_ports = ports.resolve_ports(index, cfg.ports)
    _check_port_collisions(resolved_name, new_ports)
    registry.add(resolved_name, target_root, index)
    typer.echo(
        f"[beetroot] registered {resolved_name} at {target_root} "
        f"(index {index}, ADB localhost:{new_ports['adb']}, "
        f"Frida localhost:{new_ports['frida']})"
    )


@app.command()
def apply(
    name: Annotated[str, typer.Argument(help="Instance name.")],
) -> None:
    """Re-render ``.env`` and re-stage files from the instance's beetroot.yaml."""
    _ensure_exists(name)
    root = _instance_root(name)
    cfg = config.load_yaml(paths.instance_yaml(root))
    meta = registry.get(name)
    assert meta is not None
    new_ports = ports.resolve_ports(meta["index"], cfg.ports)
    _check_port_collisions(name, new_ports)
    _stage_instance(name, root, cfg)
    typer.echo(f"[beetroot] re-staged {name} from beetroot.yaml")
    typer.echo(f"[beetroot] restart with: beetroot down {name} && beetroot up {name}")


@app.command()
def up(
    names: Annotated[
        list[str] | None,
        typer.Argument(help="Instance names to start."),
    ] = None,
    build: Annotated[
        bool,
        typer.Option("--build", help="Rebuild the image first."),
    ] = False,
    all_: Annotated[
        bool,
        typer.Option("--all", help="Act on all registered instances."),
    ] = False,
) -> None:
    """Start one or more instances."""
    for instance_name in _resolve_names(list(names or []), all_):
        _ensure_exists(instance_name)
        root = _instance_root(instance_name)
        compose.up(instance_name, root, build=build)
        meta = registry.get(instance_name)
        assert meta is not None
        cfg = config.load_yaml(paths.instance_yaml(root))
        p = ports.resolve_ports(meta["index"], cfg.ports)
        typer.echo(
            f"[beetroot] {instance_name} up — "
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
        compose.down(instance_name, _instance_root(instance_name))
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
        root = _instance_root(instance_name)
        compose.down(instance_name, root)
        compose.up(instance_name, root)
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
    root = _instance_root(name)
    if not yes:
        ans = input(f"Destroy {name} and delete {root}? [y/N] ").strip().lower()
        if ans != "y":
            typer.echo("[beetroot] aborted")
            return
    try:
        compose.down(name, root, volumes=True)
    except compose.ComposeError as e:
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
    instances = registry.list_instances()
    if json_out:
        out = {}
        for instance_name, meta in instances.items():
            root = Path(meta["absolute_path"])
            cfg = config.load_yaml(paths.instance_yaml(root))
            p = ports.resolve_ports(meta["index"], cfg.ports)
            out[instance_name] = {
                "path": str(root),
                "index": meta["index"],
                "adb": f"localhost:{p['adb']}",
                "frida": f"localhost:{p['frida']}",
                "status": compose.ps_status(instance_name, root),
                "created_at": meta["created_at"],
            }
        typer.echo(json.dumps(out, indent=2, sort_keys=True))
        return

    if not instances:
        typer.echo("(no instances — try `beetroot create alpha`)")
        return
    typer.echo(f"{'NAME':<14}{'IDX':<5}{'ADB':<22}{'FRIDA':<22}{'STATUS':<14}{'PATH'}")
    for instance_name in sorted(instances):
        meta = instances[instance_name]
        root = Path(meta["absolute_path"])
        cfg = config.load_yaml(paths.instance_yaml(root))
        p = ports.resolve_ports(meta["index"], cfg.ports)
        typer.echo(
            f"{instance_name:<14}{meta['index']:<5}"
            f"{'localhost:' + str(p['adb']):<22}"
            f"{'localhost:' + str(p['frida']):<22}"
            f"{compose.ps_status(instance_name, root):<14}"
            f"{root}"
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
    compose.logs(name, _instance_root(name), follow=follow)


@app.command()
def shell(
    name: Annotated[str, typer.Argument(help="Instance name.")],
) -> None:
    """Open an interactive ADB shell into an instance."""
    _ensure_exists(name)
    meta = registry.get(name)
    assert meta is not None
    root = _instance_root(name)
    cfg = config.load_yaml(paths.instance_yaml(root))
    p = ports.resolve_ports(meta["index"], cfg.ports)
    if shutil.which("adb") is None:
        raise _error("adb not found on PATH (install android-tools).")
    target = f"localhost:{p['adb']}"
    subprocess.run(["adb", "connect", target], check=False)
    subprocess.run(["adb", "-s", target, "shell"], check=False)


@app.command()
def env(
    name: Annotated[str, typer.Argument(help="Instance name.")],
) -> None:
    """Print eval-able ``ANDROID_DEVICE`` / ``FRIDA_DEVICE`` shell exports."""
    _ensure_exists(name)
    meta = registry.get(name)
    assert meta is not None
    root = _instance_root(name)
    cfg = config.load_yaml(paths.instance_yaml(root))
    p = ports.resolve_ports(meta["index"], cfg.ports)
    typer.echo(f"export ANDROID_DEVICE=localhost:{p['adb']}")
    typer.echo(f"export FRIDA_DEVICE=localhost:{p['frida']}")


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
    meta = registry.get(name)
    assert meta is not None
    root = _instance_root(name)
    cfg = config.load_yaml(paths.instance_yaml(root))
    p = ports.resolve_ports(meta["index"], cfg.ports)
    if shutil.which("frida") is None:
        raise _error(
            "frida CLI not found. "
            "Install via `uv tool install 'beetroot[frida]'` or `uv tool install frida-tools`."
        )
    cmd = ["frida", "-H", f"localhost:{p['frida']}", *ctx.args]
    subprocess.run(cmd, check=False)


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
    root = _instance_root(name)
    cfg = config.load_yaml(paths.instance_yaml(root))
    digest: str | None = sha256 or None
    if source.startswith(("http://", "https://")):
        cfg.modules.append(config.Module(url=source, sha256=digest))
    else:
        cfg.modules.append(config.Module(path=source, sha256=digest))
    config.write_yaml(paths.instance_yaml(root), cfg)
    modules_dl.stage_for_instance(root, cfg)
    typer.echo(f"[beetroot] added module → {paths.instance_yaml(root)}")
    typer.echo(f"[beetroot] restart to flash: beetroot down {name} && beetroot up {name}")


@app.command()
def setup(
    gapps: Annotated[
        _GappsVariant,
        typer.Argument(help="GMS variant to bake into the base image."),
    ] = _GappsVariant.lite,
) -> None:
    """Build the redroid base image and Beetroot layer for a gapps variant."""
    tag = setup_runner.bootstrap_base_image(gapps=gapps.value)
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
    root = _instance_root(name)
    dest = output if output is not None else Path(f"{name}.tar.zst")
    try:
        final = snapshot_mod.snapshot(root, dest)
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
    meta = registry.get(dest_name)
    assert meta is not None
    cfg = config.load_yaml(paths.instance_yaml(restored))
    p = ports.resolve_ports(meta["index"], cfg.ports)
    typer.echo(
        f"[beetroot] restored {dest_name} at {restored} "
        f"(index {meta['index']}, ADB localhost:{p['adb']}, Frida localhost:{p['frida']})"
    )
    typer.echo(f"[beetroot] next: beetroot apply {dest_name} && beetroot up {dest_name}")


def main() -> None:
    """
    Parse CLI arguments and dispatch to the appropriate command handler.

    Wraps ``app()`` to convert two domain exceptions raised from deep in
    the procedural call tree into the same friendly ``error: ...`` line
    + ``exit 1`` shape the rest of the CLI uses.
    """
    try:
        app()
    except paths.InstanceRootNotFoundError as e:
        typer.echo(f"error: {e}", err=True)
        sys.exit(1)
    except ports.PortCollisionError as e:
        typer.echo(f"error: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

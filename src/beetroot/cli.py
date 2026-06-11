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
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Annotated, cast

import typer
from rich.console import Console as _RichConsole

from . import api, builder, compose, console, modules_download, paths, ports, registry
from . import snapshot as snapshot_mod
from .backends import adb as adb_backend

# Instance-name regex mirrors ``api._INSTANCE_NAME_RE`` — used to
# validate names auto-derived from adb serials in :func:`adopt` before
# any registry side-effect runs.
_INSTANCE_NAME_RE = api._INSTANCE_NAME_RE  # noqa: SLF001  # intentional re-use of the api-layer regex so the two stay in lock-step


class _GappsVariant(StrEnum):
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


def _require(backend: api.DeviceBackend, cap: type, verb: str) -> object:
    """
    Return ``backend`` narrowed to ``cap``, or raise :class:`api.BackendCapabilityError`.

    This is the single gate through which all capability-gated verbs
    pass — instead of ``isinstance(b, api.Instance)`` scattered across
    every verb, every verb calls ``_require(backend, Lifecycle, "up")``
    and the error message is automatically correct for any backend kind.

    Args:
        backend: The resolved backend.
        cap: The capability sub-protocol class (e.g. :class:`api.Lifecycle`).
        verb: The verb name (used in the error message).

    Returns:
        ``backend`` narrowed to ``cap`` so callers can call cap methods directly.

    Raises:
        api.BackendCapabilityError: If ``backend`` does not satisfy ``cap``.
    """
    if isinstance(backend, cap):
        return backend
    raise api.BackendCapabilityError(
        f"{verb!r} is not supported by the {backend.kind!r} backend "
        f"for instance {backend.name!r}."
    )


def _resolve_names(names: list[str], all_flag: bool) -> list[str]:
    """
    Return the list of instance names from --all or positional names.

    Raises typer.Exit on conflicting or missing arguments. The
    all_flag + empty-registry path exits with code 0 (informational
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


def _resolve_lifecycle_names(names: list[str], all_flag: bool, verb: str) -> list[str]:
    """
    Like _resolve_names but when --all is used, skip names we can't act on.

    Single-name invocations still raise (BackendCapabilityError for a
    non-Lifecycle backend, InstanceNotFoundError for an orphan/unresolvable
    row) so the user gets a clear error for a bad explicit name. Only the
    --all fan-out skips bad rows — printing one "skipped <name>" advisory to
    stderr per skip. Two skip reasons exist: a row that resolves to a
    non-Lifecycle backend (e.g. adb), and a row that raises
    InstanceNotFoundError (a redroid orphan whose beetroot.yaml is gone, or an
    unresolvable/unknown backend kind). The latter would otherwise raise before
    the Lifecycle filter runs, aborting the whole fan-out. Note this skip is
    scoped to InstanceNotFoundError only: a present-but-unparseable
    beetroot.yaml raises ValidationError/YAMLError and is intentionally left to
    surface loudly rather than skipped.

    Args:
        names: Explicit instance names from positional args.
        all_flag: Whether --all was passed.
        verb: Verb name, used in the BackendCapabilityError message.

    Returns:
        List of names that should be acted on (all Lifecycle-capable).
    """
    raw = _resolve_names(names, all_flag)
    if not all_flag:
        return raw
    # Filter out non-Lifecycle and unresolvable backends; warn and skip them.
    filtered: list[str] = []
    for instance_name in raw:
        try:
            backend = api.Manager.resolve(instance_name)
        except api.InstanceNotFoundError as e:
            typer.echo(f"skipped {instance_name}: {e}", err=True)
            continue
        if isinstance(backend, api.Lifecycle):
            filtered.append(instance_name)
        else:
            typer.echo(
                f"skipped {instance_name} ({backend.kind}): "
                f"{verb!r} not supported by this backend",
                err=True,
            )
    return filtered


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

    The new beetroot.yaml is the minimal valid config (api_version
    plus android.version); every other field falls back to schema
    defaults. To start from a richer baseline, copy a file from the
    repo's examples/ directory over the generated beetroot.yaml.
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


def _adopt_default_name(serial: str) -> str:
    """
    Derive a deterministic default registry name from an adb serial.

    The shape is ``adb-<serial-with-colons-and-underscores-as-hyphens>``,
    lowercased, truncated to 24 chars total so the result always fits
    inside the Docker compose project-name grammar
    (``[a-z0-9_-]+``) and feels reasonable at the CLI. Truncation is
    deterministic — a user who runs ``beetroot adopt <long-serial>``
    twice gets the same name both times — so collision detection in
    the registry produces a friendly "already exists" error rather
    than a silent overwrite.

    Args:
        serial: The adb serial / endpoint identifier.

    Returns:
        A registry name guaranteed to match ``_INSTANCE_NAME_RE``.
    """
    munged = serial.replace(":", "-").replace("_", "-").lower()
    candidate = f"adb-{munged}"[:24]
    return candidate.rstrip("-") or "adb-device"


@app.command()
def adopt(
    serial: Annotated[
        str,
        typer.Argument(help="adb serial (e.g. emulator-5554 or 192.168.1.10:5555)."),
    ],
    name: Annotated[
        str | None,
        typer.Option("--name", help="Registry name (default: adb-<serial>)."),
    ] = None,
    verify: Annotated[
        bool,
        typer.Option(
            "--verify", "-V",
            help="Refuse to register if the serial is not listed as 'device' in `adb devices`.",
        ),
    ] = False,
) -> None:
    """
    Adopt a rooted Android device that's already reachable via adb.

    Allocates a Beetroot port index for the device (so a follow-up
    beetroot install_frida <name> and beetroot frida <name>
    pick the same Frida port a redroid instance with the same index
    would have got), then writes an adb-kind row to the registry.
    Unlike beetroot create, no on-disk instance directory is made;
    the device is managed by whatever installed it (real phone, third-
    party emulator, adb connect from a network device).

    Pass --verify to require the serial to be reachable via
    adb devices before the registry row is written.
    """
    resolved_name = name if name is not None else _adopt_default_name(serial)
    if not _INSTANCE_NAME_RE.fullmatch(resolved_name):
        raise _error(
            f"derived instance name {resolved_name!r} is invalid — "
            r"must match [a-z0-9_-]+. Pass --name explicitly to override.",
        )
    if registry.get(resolved_name) is not None:
        raise _error(
            f"instance {resolved_name!r} already registered. Use a different "
            f"--name, or `beetroot destroy {resolved_name}` first.",
        )
    if verify:
        if shutil.which(adb_backend._ADB) is None:  # noqa: SLF001  # needed to call the guard at verify-time before registration
            raise _error("adb not found on PATH (install android-tools)")
        if not adb_backend.serial_is_available(serial):
            raise _error(
                f"serial {serial!r} is not listed as 'device' in `adb devices`; "
                "connect the device and retry, or omit --verify to register without checking.",
            )
    backend_config = registry.AdbBackendConfig(serial=serial)
    index = registry.add_allocating(resolved_name, backend=backend_config)
    typer.echo(
        f"[beetroot] adopted {resolved_name} → adb serial {serial} "
        f"(index {index})"
    )
    typer.echo(
        f"[beetroot] next: beetroot shell {resolved_name} "
        f"(or `beetroot frida {resolved_name}` once frida-server is running)"
    )


@app.command()
def apply(
    name: Annotated[str, typer.Argument(help="Instance name.")],
) -> None:
    """Re-render .env and re-stage files from the instance's beetroot.yaml."""
    _ensure_exists(name)
    backend = api.Manager.resolve(name)
    lc = cast(api.Lifecycle, _require(backend, api.Lifecycle, "apply"))
    try:
        lc.apply()
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
    build: Annotated[
        bool,
        typer.Option(
            "--build",
            hidden=True,
            help="(removed in v0.3 — see CHANGELOG.md)",
        ),
    ] = False,
) -> None:
    """Start one or more instances."""
    if build:
        # T5 removed --build from up (build vs. start are two concerns),
        # but Typer rejected the v0.2-shape invocation with a Rich
        # "No such option: --build" box. The hidden alias is purely for
        # the friendlier migration hint.
        raise _error(
            "'beetroot up --build' was removed in v0.3 — "
            "run 'beetroot build' separately first to rebuild the image."
        )
    for instance_name in _resolve_lifecycle_names(list(names or []), all_, "up"):
        _ensure_exists(instance_name)
        backend = api.Manager.resolve(instance_name)
        lc = cast(api.Lifecycle, _require(backend, api.Lifecycle, "up"))
        lc.up()
        if isinstance(backend, api.Instance):
            p = backend.ports
            typer.echo(
                f"[beetroot] {backend.name} up — "
                f"ADB localhost:{p['adb']}, Frida localhost:{p['frida']}"
            )
        else:
            typer.echo(f"[beetroot] {instance_name} up")


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
    for instance_name in _resolve_lifecycle_names(list(names or []), all_, "down"):
        _ensure_exists(instance_name)
        backend = api.Manager.resolve(instance_name)
        cast(api.Lifecycle, _require(backend, api.Lifecycle, "down")).down()
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
    for instance_name in _resolve_lifecycle_names(list(names or []), all_, "restart"):
        _ensure_exists(instance_name)
        backend = api.Manager.resolve(instance_name)
        cast(api.Lifecycle, _require(backend, api.Lifecycle, "restart")).restart()
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
    # Prompt for confirmation here in the CLI, not in the library.
    # Instance.destroy(yes=True) is always passed once the user has
    # confirmed here — the library API must not prompt on stdin.
    if not yes:
        confirmed = typer.confirm(
            f"Destroy {name} and all its data? This cannot be undone.",
            default=False,
        )
        if not confirmed:
            typer.echo("[beetroot] aborted")
            return
    # Try resolving the backend first so we can gate on the Lifecycle
    # sub-protocol. If resolution fails with InstanceNotFoundError AND
    # the registry row is redroid-kind (orphan: yaml gone), fall through
    # to the registry-meta-based orphan cleanup. Non-redroid rows that
    # lack the Lifecycle capability get a BackendCapabilityError (exit 2),
    # pointing the user at `beetroot forget`.
    try:
        backend = api.Manager.resolve(name)
        lc = cast(api.Lifecycle, _require(backend, api.Lifecycle, "destroy"))
        try:
            lc.destroy(yes=True)
        except compose.ComposeError as e:
            # compose.down failed but host-side teardown (registry row +
            # directory) already ran inside Instance.destroy. Surface as
            # advisory so the user knows cleanup still happened.
            typer.echo(f"[beetroot] (compose down failed: {e}; continuing)")
        typer.echo(f"[beetroot] destroyed {name}")
        return
    except api.InstanceNotFoundError:
        pass  # fall through to orphan-cleanup path below

    # Orphan path: redroid row whose beetroot.yaml is gone. Manager.resolve
    # raises InstanceNotFoundError for these because Instance.load() trips
    # on the missing yaml. We check the registry kind here: non-redroid
    # orphans (shouldn't exist, but be safe) just get the registry row
    # removed; redroid orphans also run the compose/dir cleanup.
    meta = registry.get(name)
    if meta is None:  # pragma: no cover  # _ensure_exists ran upstream; defensive net
        raise _error(f"no instance named {name!r}. Try `beetroot ls`.")
    if not isinstance(meta.backend, registry.RedroidBackendConfig):
        # A non-redroid row that Manager.resolve couldn't build — surface
        # a helpful error pointing at `beetroot forget`.
        raise api.BackendCapabilityError(
            f"'destroy' is not supported by the {meta.backend.kind!r} backend "
            f"for instance {name!r}."
        )
    root = registry.instance_path(name)
    if root.exists():
        try:
            compose.down(name, root, volumes=True)
        except compose.ComposeError as e:
            # Surface the compose failure as a "continuing" advisory so
            # the user knows the host-side cleanup still ran.
            typer.echo(f"[beetroot] (compose down failed: {e}; continuing)")
        # Remove the registry row BEFORE deleting the directory so an
        # interrupt between the two operations always leaves a clean
        # state: a registered-but-deleted instance (row first) is
        # detectable as an orphan; a deleted-then-missing-row instance
        # would silently leak the port index. Mirror the invariant in
        # api.py's _teardown_under_lock.
        registry.remove(name)
        shutil.rmtree(root)
    else:
        # Orphan registry entry — the on-disk dir is already gone, so
        # compose.down would FileNotFoundError on its cwd= arg.
        # Skip it and just clean the registry row.
        typer.echo(
            f"[beetroot] (instance dir {root} already gone; "
            f"removing orphan registry entry)"
        )
        registry.remove(name)
    typer.echo(f"[beetroot] destroyed {name}")


@app.command()
def forget(
    name: Annotated[str, typer.Argument(help="Instance name to deregister.")],
) -> None:
    """
    Deregister an instance from the registry without touching its host directory.

    Removes the registry row and frees its port index. No host-directory
    teardown, no docker compose down, no data deletion — it is the
    inverse of beetroot adopt (and the safe cleanup path for adb-backed
    instances that beetroot destroy refuses to handle). Works for
    both redroid and adb instances.
    """
    _ensure_exists(name)
    registry.remove(name)
    typer.echo(f"[beetroot] forgot {name} (registry row removed; host directory untouched)")


@app.command(name="ls")
def ls(
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON instead of the human-readable table."),
    ] = False,
) -> None:
    """List all registered instances with their ports and live status."""
    instances = api.Manager.list_instances()
    orphans = api.Manager.list_orphans()
    if json_out:
        # JSON must go to plain stdout (not through rich) so downstream
        # parsers never see ANSI markup. Orphan advisories go to stderr
        # so they don't pollute the JSON stream.
        out = {inst.name: _instance_json_row(inst) for inst in instances}
        print(json.dumps(out, indent=2, sort_keys=True))  # noqa: T201  # plain JSON stdout — must not go through rich
        if orphans:
            _emit_orphan_skip(orphans)
        return

    if not instances and not orphans:
        typer.echo("(no instances — try 'beetroot create alpha')")
        return
    if instances:
        # Inject a runtime-bound Console so the table writes to the
        # current sys.stdout (e.g. CliRunner's StringIO in tests, or the
        # real terminal in production). The module-level singleton is
        # bound at import time and doesn't pick up CliRunner redirections.
        _runtime_console = _RichConsole(file=sys.stdout, highlight=False)
        old_stdout_console = console._stdout_console  # noqa: SLF001  # intentional injection for runtime sys.stdout binding
        console.set_consoles(stdout=_runtime_console)
        try:
            console.table(
                columns=["NAME", "IDX", "ADB", "FRIDA", "STATUS", "PATH"],
                rows=[
                    [
                        inst.name,
                        str(inst.index),
                        f"localhost:{inst.ports['adb']}",
                        f"localhost:{inst.ports['frida']}",
                        str(inst.status),
                        str(inst.root),
                    ]
                    for inst in instances
                ],
            )
        finally:
            console.set_consoles(stdout=old_stdout_console)
    if orphans:
        _emit_orphan_skip(orphans)


def _emit_orphan_skip(orphans: list[str]) -> None:
    """Print the trailing orphan advisory to stderr so it never pollutes JSON output."""
    names = ", ".join(orphans)
    typer.echo(
        f"(skipping {len(orphans)} orphan "
        f"{'entry' if len(orphans) == 1 else 'entries'}: {names}; "
        f"clean up with 'beetroot destroy <name> -y')",
        err=True,
    )


def _instance_json_row(inst: api.Instance) -> dict[str, object]:
    """
    Build the per-instance JSON row used by ``ls --json`` and ``status``.

    Keeps the v0.3 ``path`` / ``adb`` / ``frida`` keys for back-compat
    (existing scripts pipe ``ls --json`` through jq with those keys);
    layers on the v0.4 spec fields (``kind``, ``adb_address``,
    ``frida_address``, ``stealth_paths``, full ``ports`` dict) so
    ``beetroot status`` is a one-stop machine-parseable snapshot of
    everything the registry + live state knows about the instance.
    """
    p = inst.ports
    meta = registry.get(inst.name)
    # Manager.list already filtered orphans; this branch is a defensive
    # net against a registry race and isn't covered.
    if meta is None:  # pragma: no cover
        raise registry.RegistryError(
            f"instance {inst.name!r} disappeared from the registry",
        )
    backend = meta.backend
    if not isinstance(backend, registry.RedroidBackendConfig):  # pragma: no cover
        # ``Instance.load`` only ever returns redroid-kind, so this is
        # a defensive narrowing aid for mypy. The adb-row path is in
        # ``_adb_json_row``.
        raise registry.RegistryError(
            f"instance {inst.name!r} is not a redroid backend",
        )
    return {
        "name": inst.name,
        "kind": inst.kind,
        "index": inst.index,
        "created_at": meta.created_at.isoformat(),
        "ports": p,
        "status": inst.status,
        "adb_address": inst.adb_address,
        "frida_address": inst.frida_address,
        "stealth_paths": dict(backend.stealth_paths),
        # v0.3 back-compat keys — scripts piping ``ls --json`` through
        # jq depend on these. Kept alongside the v0.4 richer fields so
        # the row is a strict superset of the v0.3 shape.
        "path": str(inst.root),
        "adb": f"localhost:{p['adb']}",
        "frida": f"localhost:{p['frida']}",
    }


def _adb_json_row(
    name: str, meta: registry.InstanceMeta, backend: api.DeviceBackend,
) -> dict[str, object]:
    """
    Build the per-instance JSON row for a non-redroid backend.

    Uses the resolved backend (not the raw registry config) so
    frida_address and adb_address come from the backend's Protocol
    surface — meaning an adb device at index 1 reports the correct
    index-1 frida port rather than a hardcoded 27042.
    """
    row: dict[str, object] = {
        "name": name,
        "kind": backend.kind,
        "index": meta.index,
        "created_at": meta.created_at.isoformat(),
        "adb_address": backend.adb_address,
        "frida_address": backend.frida_address,
        "is_available": backend.is_available,
        "stealth_paths": {},
    }
    # For adb-kind backends, include the serial so scripts that check
    # row["serial"] can distinguish this row from a redroid instance.
    serial = getattr(meta.backend, "serial", None)
    if serial is not None:  # pragma: no branch  # only AdbBackendConfig reaches here today
        row["serial"] = serial
    return row


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
    backend = api.Manager.resolve(name)
    inst = cast(api.Instance, _require(backend, api.Instance, "logs"))
    inst.logs(follow=follow)


@app.command(
    name="shell",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def shell(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Instance name.")],
) -> None:
    """
    Open an interactive ADB shell, optionally running a one-shot command.

    Extra arguments after the instance name are forwarded to the underlying
    adb shell. Use -c 'cmd' to run a non-interactive command:

        beetroot shell alpha -c 'id'
        beetroot shell alpha -c 'ls /data/local/tmp'
    """
    _ensure_exists(name)
    backend = api.Manager.resolve(name)
    extra: Sequence[str] = list(ctx.args)
    try:
        rc = backend.shell(extra or None)
    except api.AdbNotInstalledError as e:
        raise _error(str(e)) from e
    if rc != 0:
        # Propagate the subprocess exit code so research scripts that
        # check $? after beetroot shell <name> -c '<cmd>' see the
        # underlying adb shell status.
        raise typer.Exit(code=rc)


@app.command()
def status(
    name: Annotated[str, typer.Argument(help="Instance name.")],
) -> None:
    """
    Print a single-instance JSON snapshot.

    Output is JSON to stdout — pipe to jq for selective fields. The row
    shape is the same as ls --json for redroid; adb-kind entries get a
    smaller row with serial and the correct allocated frida_address
    instead of absolute_path.

    Exit codes: 0 on success, 1 if name is not in the registry.
    """
    _ensure_exists(name)
    backend = api.Manager.resolve(name)
    if isinstance(backend, api.Instance):
        row = _instance_json_row(backend)
    else:
        # Generic backend path — build the status row from Protocol surface
        # so adb and third-party backends report real addresses (the old
        # _adb_json_row hardcoded frida_address as the serial; AdbDevice
        # now returns the correctly-allocated forwarded port).
        meta = registry.get(name)
        if meta is None:  # pragma: no cover  # _ensure_exists ran upstream
            raise _error(f"no instance named {name!r}")
        row = _adb_json_row(name, meta, backend)
    print(json.dumps(row, indent=2, sort_keys=True))  # noqa: T201  # plain JSON stdout — must not go through rich


@app.command()
def doctor(
    name: Annotated[str, typer.Argument(help="Instance name.")],
) -> None:
    """
    Run the aggregated health checks for an instance.

    Output is machine-parseable: one "<check>: <status> [reason]"
    line per check. "pass" rows elide the reason; "fail" and
    "skip" rows include it.

    Exits 0 if every check passes; otherwise the exit code is the count
    of "fail" results (capped at 255 — the POSIX exit-code ceiling).
    "skip" rows do not count toward the exit code.
    """
    _ensure_exists(name)
    backend = api.Manager.resolve(name)
    hc = cast(api.HealthCheckable, _require(backend, api.HealthCheckable, "doctor"))
    results = hc.health()
    fail_count = 0
    for check_name, result in results.items():
        if result.status == "pass":
            line = f"{check_name}: pass"
        else:
            reason = f" {result.reason}" if result.reason else ""
            line = f"{check_name}: {result.status}{reason}"
        if result.status == "fail":
            fail_count += 1
        typer.echo(line)
    if fail_count > 0:
        # POSIX exit codes top out at 255; clamp so a hypothetical
        # 300-check fan-out doesn't wrap to 44.
        raise typer.Exit(code=min(fail_count, 255))


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

    Any tokens after <name> (e.g. -n com.app, -f com.app -l script.js)
    are passed verbatim to the underlying frida CLI, after Beetroot prepends
    -H localhost:<frida_port>. Use -- to disambiguate frida flags from
    Typer's own option-parsing if needed.
    """
    _ensure_exists(name)
    backend = api.Manager.resolve(name)
    try:
        rc = backend.frida_cli(list(ctx.args))
    except api.FridaNotInstalledError as e:
        raise _error(str(e)) from e
    if rc != 0:
        # Propagate the subprocess exit code so research scripts that
        # check ``$?`` after ``beetroot frida <name> ...`` see the
        # underlying ``frida`` status.
        raise typer.Exit(code=rc)


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
    backend = api.Manager.resolve(name)
    installer = cast(api.ModuleInstaller, _require(backend, api.ModuleInstaller, "module"))
    installer.add_module(source, sha256=sha256)
    if isinstance(backend, api.Instance):
        typer.echo(f"[beetroot] added module → {paths.instance_yaml(backend.root)}")
        typer.echo(
            f"[beetroot] restart to flash: beetroot down {name} && beetroot up {name}"
        )
    else:
        typer.echo(f"[beetroot] module pushed to {name}")


@app.command(name="setup", hidden=True)
def setup_deprecated(
    args: Annotated[
        list[str] | None,
        typer.Argument(help="(removed in v0.3)"),
    ] = None,
) -> None:
    """
    Print a migration hint for the v0.2 ``setup`` verb.

    v0.2 had ``beetroot setup [variant]``; v0.3 renamed it to
    ``beetroot build``. This hidden alias catches the old form and
    surfaces a one-line migration message instead of bare Typer
    ``No such command`` output.
    """
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
    """Pack an instance's host-side state into a .tar.zst archive."""
    _ensure_exists(name)
    backend = api.Manager.resolve(name)
    snappable = cast(api.Snapshottable, _require(backend, api.Snapshottable, "snapshot"))
    dest = output if output is not None else Path(f"{name}.tar.zst")
    try:
        final = snappable.snapshot(dest)
    except snapshot_mod.SnapshotError as e:
        raise _error(str(e)) from e
    typer.echo(f"[beetroot] snapshot of {name} → {final}")


@app.command(name="restore")
def restore(
    archive: Annotated[
        Path,
        typer.Argument(help="Path to a .tar.zst snapshot archive."),
    ],
    name: Annotated[
        str | None,
        typer.Option("--name", help="Registry name for the restored instance."),
    ] = None,
    as_: Annotated[
        str | None,
        typer.Option(
            "--as",
            hidden=True,
            help="(deprecated alias for --name; removed in v0.7)",
        ),
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
    # --as is a one-release hidden back-compat alias for --name.
    # If both are given, --name wins (--as is for migration only).
    effective_name = name if name is not None else as_
    try:
        manifest = snapshot_mod.read_manifest(archive)
    except snapshot_mod.SnapshotError as e:
        raise _error(str(e)) from e
    dest_name = effective_name if effective_name is not None else manifest.name
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
    # Agent A's fix made ``snapshot.restore`` call ``_stage()``
    # itself, so an intermediate ``beetroot apply`` is no longer
    # required before ``beetroot up``. CR #3 finding 10.
    typer.echo(f"[beetroot] next: beetroot up {dest_name}")


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
    except api.BackendCapabilityError as e:
        # Exit code 2 (distinct from "instance not found" / domain
        # error → 1) so scripts wrapping the CLI can distinguish
        # "this verb doesn't apply to this backend" from "this
        # instance / file / network call failed". v0.4 introduces
        # backend-typed exit codes; the rest stay 1 for source compat.
        typer.echo(f"error: {e}", err=True)
        sys.exit(2)
    except api.InstanceNotFoundError as e:
        # Manager.resolve raises InstanceNotFoundError for unknown names
        # and for unresolvable backend kinds (e.g. package not installed).
        # v0.4 let these propagate as tracebacks; v0.6 catches them for
        # a friendly error: ... line + exit 1.
        typer.echo(f"error: {e}", err=True)
        sys.exit(1)
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
    except modules_download.ModuleFetchError as e:
        typer.echo(f"error: {e}", err=True)
        sys.exit(1)
    except registry.RegistryError as e:
        # T2 Agent 3 1.9: any code path that walks the registry can
        # surface a RegistryError ("unknown instance X", "X is an
        # adb backend, no on-disk dir") that v0.3 let propagate as
        # a Rich-rendered traceback. Catch it alongside the other
        # domain exceptions for a friendly ``error: ...`` line.
        typer.echo(f"error: {e}", err=True)
        sys.exit(1)
    except FileNotFoundError as e:
        # Belt-and-suspenders: an instance whose on-disk dir was
        # ``rm -rf``'d behind the CLI's back leaves a stale registry
        # entry; ``Instance.load`` then trips on the missing
        # ``beetroot.yaml`` deep in the call tree. ``Manager.list``
        # filters orphans itself, but a verb that targets the orphan
        # by name still needs this safety net.
        typer.echo(f"error: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

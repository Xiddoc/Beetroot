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
import os
import shutil
import sys
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Annotated, cast

import pydantic
import typer
import yaml

from . import (
    api,
    builder,
    capabilities,
    compose,
    config,
    console,
    hostcheck,
    modules_download,
    paths,
    ports,
    registry,
)
from . import snapshot as snapshot_mod
from .backends import adb as adb_backend
from .backends import vm as vm_backend
from .vm import qemu as vm_qemu

# Instance-name regex mirrors ``api._INSTANCE_NAME_RE`` — used to
# validate names auto-derived from adb serials in :func:`adopt` before
# any registry side-effect runs.
_INSTANCE_NAME_RE = api._INSTANCE_NAME_RE  # noqa: SLF001  # intentional reuse of the api-layer regex so the two stay in lock-step


class _GappsVariant(StrEnum):
    """
    GMS variants accepted by ``beetroot build``.
    """

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
    Print a styled ``error: <message>`` to stderr and return a ``typer.Exit(1)``.

    Callers ``raise _error(...)`` so mypy keeps narrowing on the no-return
    branch and so the ``error: ...`` line lands on stderr (matching the
    old ``sys.exit("error: ...")`` behavior, which wrote to stderr). The
    line is rendered through :func:`console.error`, so it is red on a TTY and
    plain ``error: <message>`` everywhere else.
    """
    console.error(message)
    return typer.Exit(code=1)


def _ensure_exists(name: str) -> None:
    if registry.get(name) is None:
        raise _error(f"no instance named {name!r}. Try `beetroot ls`.")


def _load(name: str) -> api.Instance:
    """
    Load an instance for a verb that has already passed ``_ensure_exists``.
    """
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
        f"{verb!r} is not supported by the {backend.kind!r} backend for instance {backend.name!r}."
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
            console.hint("(no instances)")
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
            console.note(f"skipped {instance_name}: {e}")
            continue
        if isinstance(backend, api.Lifecycle):
            filtered.append(instance_name)
        else:
            console.note(
                f"skipped {instance_name} ({backend.kind}): {verb!r} not supported by this backend",
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
        console.step(f"copying {src} → {dst}")
        shutil.copytree(src, dst)

    console.step(f"allocating a port index and staging files for {name}")
    try:
        inst = api.Instance.create(name, path=target_root)
    except ValueError as e:
        raise _error(str(e)) from e
    p = inst.ports
    console.status(
        f"created {inst.name} at {inst.root} "
        f"(index {inst.index}, ADB localhost:{p['adb']}, Frida localhost:{p['frida']})"
    )
    console.hint(f"next: beetroot up {inst.name}")


@app.command()
def register(
    path: Annotated[Path, typer.Argument(help="Path to a directory containing beetroot.yaml.")],
    name: Annotated[
        str | None,
        typer.Option("--name", help="Registry name (default: basename of path)."),
    ] = None,
) -> None:
    """
    Adopt an existing instance directory under the global registry.
    """
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
    console.status(
        f"registered {inst.name} at {inst.root} "
        f"(index {inst.index}, ADB localhost:{p['adb']}, "
        f"Frida localhost:{p['frida']})"
    )
    console.hint(f"next: beetroot up {inst.name}")


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
            "--verify",
            "-V",
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
    console.status(f"adopted {resolved_name} → adb serial {serial} (index {index})")
    console.hint(
        f"next: beetroot shell {resolved_name} "
        f"(or `beetroot frida {resolved_name}` once frida-server is running)"
    )


@app.command()
def apply(
    name: Annotated[str, typer.Argument(help="Instance name.")],
) -> None:
    """
    Re-render .env and re-stage files from the instance's beetroot.yaml.
    """
    _ensure_exists(name)
    backend = api.Manager.resolve(name)
    lc = cast(api.Lifecycle, _require(backend, api.Lifecycle, "apply"))
    console.step(f"re-rendering .env and staging files for {name} from beetroot.yaml")
    try:
        lc.apply()
    except ValueError as e:
        raise _error(str(e)) from e
    console.status(f"re-staged {name} from beetroot.yaml")
    console.hint(f"restart with: beetroot down {name} && beetroot up {name}")


def _binder_preflight(mode: hostcheck.BinderMode, *, warned: bool) -> bool:
    """
    Apply a redroid instance's configured binder mode before starting it.

    Folds the instance's ``binder`` mode against the live host probe via
    :func:`hostcheck.plan_binder_runtime` and acts on the resulting plan:

    * ``proceed`` — silent; the host can satisfy binder.
    * ``warn`` (lenient ``auto`` mode, host can't provide binder) — print
      the one-line advisory at most once across an ``up`` fan-out and
      start anyway (``docker compose up -d`` succeeds but Android may not
      boot — the advisory is the only symptom otherwise).
    * ``block`` (strict ``host`` mode, host can't provide binder) —
      ``raise _error`` so the user gets a fast, actionable failure instead
      of a container that never boots.

    The ``vm`` plan action is handled by the VM backend's own banner in the
    ``up`` verb, not here: a ``binder: vm`` instance resolves to
    :class:`beetroot.backends.vm.VmDeviceBackend`, never an
    :class:`api.Instance`, so this redroid-only preflight only ever sees the
    ``auto`` / ``host`` modes.

    Args:
        mode: The instance's configured binder mode (``cfg.binder``).
        warned: Whether the advisory already printed earlier in this
            fan-out (dedup across multiple instances).

    Returns:
        The updated ``warned`` flag (True once the advisory has printed).

    Raises:
        typer.Exit: For the ``block`` plan (via :func:`_error`).
    """
    plan = hostcheck.plan_binder_runtime(mode, hostcheck.binder_status())
    if plan.action == "proceed":
        return warned
    if plan.action == "block":
        remedy = f" Remedy: {plan.remedy}." if plan.remedy else ""
        raise _error(f"{plan.reason}.{remedy}")
    if not warned:
        remedy = f" Remedy: {plan.remedy}." if plan.remedy else ""
        console.note(
            f"warning: redroid needs the kernel binder driver, but {plan.reason}. "
            f"The container may start but Android will not boot.{remedy} "
            "Run `beetroot doctor <name>` to recheck."
        )
    return True


def _vm_up_banner(backend: vm_backend.VmDeviceBackend) -> None:
    """
    Print the capability-ladder banner for a ``binder: vm`` instance.

    Implements the design doc §7 UX: ONE banner when KVM-accelerated, a
    LOUD banner (noting the ~5-20x slowdown — slow first boot is expected,
    not a hang) when the host falls back to TCG. An explicit ``accel: kvm``
    on a host without ``/dev/kvm`` raises here (via :meth:`resolved_accel`)
    so the user gets the actionable error before QEMU is even spawned.

    Args:
        backend: The resolved VM backend about to be started.

    Raises:
        typer.Exit: If ``accel: kvm`` was demanded but ``/dev/kvm`` is
            unavailable (via :func:`_error`).
    """
    try:
        accel = backend.resolved_accel()
    except vm_qemu.QemuLaunchError as e:
        raise _error(str(e)) from e
    if accel == "kvm":
        console.note(
            "backend: emulated micro-VM (no host binder) — acceleration: KVM (near-native)."
        )
        return
    console.note(
        "backend: emulated micro-VM (no host binder) — "
        "acceleration: TCG (software): /dev/kvm not available. "
        "First boot is SLOW (~5-20x; minutes, not seconds) — this is "
        "expected, not a hang. Pin `vm.accel: kvm` on a host with nested "
        "virt for near-native speed."
    )


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
    """
    Start one or more instances.
    """
    if build:
        # T5 removed --build from up (build vs. start are two concerns),
        # but Typer rejected the v0.2-shape invocation with a Rich
        # "No such option: --build" box. The hidden alias is purely for
        # the friendlier migration hint.
        raise _error(
            "'beetroot up --build' was removed in v0.3 — "
            "run 'beetroot build' separately first to rebuild the image."
        )
    binder_warned = False
    for instance_name in _resolve_lifecycle_names(list(names or []), all_, "up"):
        _ensure_exists(instance_name)
        backend = api.Manager.resolve(instance_name)
        lc = cast(api.Lifecycle, _require(backend, api.Lifecycle, "up"))
        # redroid containers need the host's binder driver to boot. The
        # per-instance ``binder`` mode decides what happens when the host
        # can't provide it: ``auto`` warns once (the container starts but
        # Android may not boot — `docker compose up -d` "succeeds"
        # regardless, so without this the only symptom is adb never
        # connecting); ``host`` fails fast; ``vm`` dispatches to the QEMU
        # micro-VM backend (a separate registry kind, handled below). adb-
        # backed instances don't need binder, so the preflight is gated on
        # the redroid kind.
        if isinstance(backend, api.Instance):
            if backend.config.binder == "vm":
                # Registry says redroid but the yaml was hand-edited to
                # ``binder: vm`` without re-applying — the two are out of
                # sync. Fail fast with the fix rather than silently starting
                # a redroid container that can't honour the vm intent.
                raise _error(
                    f"instance {instance_name!r} sets binder: vm but is still "
                    "registered as a redroid backend. Run "
                    f"`beetroot apply {instance_name}` to switch it to the "
                    "micro-VM engine, then retry `beetroot up`."
                )
            binder_warned = _binder_preflight(backend.config.binder, warned=binder_warned)
        if isinstance(backend, vm_backend.VmDeviceBackend):
            _vm_up_banner(backend)
        console.step(f"starting {instance_name}")
        try:
            lc.up()
        except vm_qemu.QemuLaunchError as e:
            raise _error(str(e)) from e
        if isinstance(backend, api.Instance | vm_backend.VmDeviceBackend):
            p = backend.ports
            console.status(
                f"{backend.name} up — ADB localhost:{p['adb']}, Frida localhost:{p['frida']}"
            )
            console.hint(f"next: beetroot shell {backend.name}")
        else:
            console.status(f"{instance_name} up")


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
    """
    Stop one or more instances, preserving data.
    """
    for instance_name in _resolve_lifecycle_names(list(names or []), all_, "down"):
        _ensure_exists(instance_name)
        backend = api.Manager.resolve(instance_name)
        console.step(f"stopping {instance_name}")
        cast(api.Lifecycle, _require(backend, api.Lifecycle, "down")).down()
        console.status(f"{instance_name} down (data preserved)")


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
    """
    Stop then start one or more instances.
    """
    for instance_name in _resolve_lifecycle_names(list(names or []), all_, "restart"):
        _ensure_exists(instance_name)
        backend = api.Manager.resolve(instance_name)
        lc = cast(api.Lifecycle, _require(backend, api.Lifecycle, "restart"))
        # A vm restart re-launches QEMU, so print the same TCG/KVM banner
        # (and surface an explicit-kvm-without-/dev/kvm error before the
        # restart) the ``up`` verb prints — restart shouldn't be a quieter
        # path to the same expensive boot.
        if isinstance(backend, vm_backend.VmDeviceBackend):
            _vm_up_banner(backend)
        console.step(f"restarting {instance_name}")
        lc.restart()
        console.status(f"{instance_name} restarted")


@app.command()
def destroy(
    name: Annotated[str, typer.Argument(help="Instance name to destroy.")],
    yes: Annotated[
        bool,
        typer.Option("-y", "--yes", help="Skip confirmation."),
    ] = False,
) -> None:
    """
    Stop and permanently delete an instance including its data directory.
    """
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
            console.status("aborted")
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
        console.step(f"tearing down {name} (stopping container, deleting data)")
        try:
            lc.destroy(yes=True)
        except compose.ComposeError as e:
            # compose.down failed but host-side teardown (registry row +
            # directory) already ran inside Instance.destroy. Surface as
            # advisory so the user knows cleanup still happened.
            console.note(f"(compose down failed: {e}; continuing)")
        console.status(f"destroyed {name}")
        return
    except api.InstanceNotFoundError:
        pass  # fall through to orphan-cleanup path below

    # Orphan path: a directory-backed (redroid / vm) row whose
    # beetroot.yaml is gone. Manager.resolve raises InstanceNotFoundError
    # for these because the backend's loader trips on the missing yaml. We
    # check the registry kind here: non-directory-backed orphans (shouldn't
    # exist, but be safe) just get the registry row removed; redroid orphans
    # also run the compose/dir cleanup, vm orphans terminate the QEMU
    # process (no compose project to tear down).
    meta = registry.get(name)
    if meta is None:  # pragma: no cover  # _ensure_exists ran upstream; defensive net
        raise _error(f"no instance named {name!r}. Try `beetroot ls`.")
    if not isinstance(meta.backend, registry.RedroidBackendConfig | registry.VmBackendConfig):
        # A non-directory-backed row that Manager.resolve couldn't build —
        # surface a helpful error pointing at `beetroot forget`. The
        # directory-backed kinds (redroid / vm — both carry absolute_path,
        # mirroring registry.instance_path / all_resolved_ports) fall
        # through to the dir-cleanup path below.
        raise api.BackendCapabilityError(
            f"'destroy' is not supported by the {meta.backend.kind!r} backend "
            f"for instance {name!r}."
        )
    is_vm_orphan = isinstance(meta.backend, registry.VmBackendConfig)
    root = registry.instance_path(name)
    if root.exists():
        if is_vm_orphan:
            # No compose project for a vm instance — terminate the QEMU
            # process via its pidfile (a no-op if it isn't running).
            vm_qemu.QemuProcess(root).terminate()
        else:
            try:
                compose.down(name, root, volumes=True)
            except compose.ComposeError as e:
                # Surface the compose failure as a "continuing" advisory so
                # the user knows the host-side cleanup still ran.
                console.note(f"(compose down failed: {e}; continuing)")
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
        console.note(f"(instance dir {root} already gone; removing orphan registry entry)")
        registry.remove(name)
    console.status(f"destroyed {name}")


@app.command()
def reset(
    name: Annotated[str, typer.Argument(help="Instance name to reset.")],
    yes: Annotated[
        bool,
        typer.Option("-y", "--yes", help="Skip confirmation."),
    ] = False,
) -> None:
    """
    Drop an instance's /data (app state) while keeping the instance and tooling.
    """
    _ensure_exists(name)
    # Prompt in the CLI, not the library — Instance.reset(yes=True) is only
    # called once the user has confirmed (the library never blocks on stdin).
    if not yes:
        confirmed = typer.confirm(
            f"Reset {name}? This wipes its /data (installed apps, accounts, "
            "flashed-module / LSPosed scope state) but keeps the instance, "
            "Frida, and modules.",
            default=False,
        )
        if not confirmed:
            console.status("aborted")
            return
    backend = api.Manager.resolve(name)
    resettable = cast(api.Resettable, _require(backend, api.Resettable, "reset"))
    console.step(f"resetting {name} (stopping container, wiping /data)")
    try:
        resettable.reset(yes=True)
    except compose.ComposeError as e:
        raise _error(f"could not stop {name} to reset it: {e}") from e
    console.status(f"reset {name} — run 'beetroot up {name}' for a fresh /data")


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
    console.status(f"forgot {name} (registry row removed; host directory untouched)")


@app.command(name="ls")
def ls(
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON instead of the human-readable table."),
    ] = False,
) -> None:
    """
    List every registered instance — redroid containers and adopted devices alike.

    Walks all backend kinds via Manager.all(), so adb-adopted devices
    appear next to redroid instances. Redroid rows report live
    docker compose ps status and the instance directory; adb rows
    report live adb devices availability, the serial in the ADB
    column, and - for PATH (no on-disk directory). Orphan entries
    are skipped with a trailing stderr advisory, as before.
    """
    rows = _ls_rows()
    orphans = api.Manager.list_orphans()
    if json_out:
        # JSON must go to plain stdout (not through rich) so downstream
        # parsers never see ANSI markup. Orphan advisories go to stderr
        # so they don't pollute the JSON stream.
        out = {name: _backend_json_row(name, meta, backend) for name, meta, backend in rows}
        print(json.dumps(out, indent=2, sort_keys=True))  # noqa: T201  # plain JSON stdout — must not go through rich
        if orphans:
            _emit_orphan_skip(orphans)
        return

    if not rows and not orphans:
        console.hint("(no instances — try 'beetroot create alpha')")
        return
    if rows:
        # The stdout console resolves ``sys.stdout`` lazily on every write, so
        # the table lands on whatever stream is current (CliRunner's StringIO
        # in tests, the real terminal in production) — no runtime rebind needed.
        console.table(
            columns=["NAME", "KIND", "IDX", "ADB", "FRIDA", "STATUS", "PATH"],
            rows=[_ls_table_row(name, meta, backend) for name, meta, backend in rows],
        )
    if orphans:
        _emit_orphan_skip(orphans)


def _ls_rows() -> list[tuple[str, registry.InstanceMeta, api.DeviceBackend]]:
    """
    Pair every resolvable backend with its registry meta, sorted by name.

    ``Manager.all()`` and the registry snapshot are two separate reads,
    so a row that vanishes between them (a concurrent ``beetroot
    forget`` / ``destroy`` in another process) is skipped rather than
    crashing ``ls``. Orphan and unresolvable rows never come back from
    ``Manager.all()`` in the first place — the orphan-skip contract is
    unchanged.
    """
    metas = registry.list_instances()
    rows: list[tuple[str, registry.InstanceMeta, api.DeviceBackend]] = []
    for backend in api.Manager.all():
        meta = metas.get(backend.name)
        if meta is None:
            continue
        rows.append((backend.name, meta, backend))
    return rows


def _backend_json_row(
    name: str,
    meta: registry.InstanceMeta,
    backend: api.DeviceBackend,
) -> dict[str, object]:
    """
    Dispatch to the kind-appropriate JSON row builder for ``ls --json``.

    Redroid instances keep the richer ``_instance_json_row`` shape
    (including the v0.3 back-compat ``path`` / ``adb`` / ``frida``
    keys); every other backend kind gets the Protocol-surface row from
    ``_adb_json_row`` — the same shape ``beetroot status`` emits.
    """
    if isinstance(backend, api.Instance):
        return _instance_json_row(backend)
    return _adb_json_row(name, meta, backend)


def _ls_table_row(
    name: str,
    meta: registry.InstanceMeta,
    backend: api.DeviceBackend,
) -> list[str]:
    """
    Render one ``beetroot ls`` table row for any backend kind.

    Redroid rows show the live compose status and the instance
    directory; non-directory-backed kinds (adb) show ``adb devices``
    availability, the serial as the ADB address, and ``-`` for PATH.
    """
    if isinstance(backend, api.Instance):
        status = str(backend.status)
        path = str(backend.root)
    else:
        status = "available" if backend.is_available else "unavailable"
        path = "-"
    return [
        name,
        backend.kind,
        str(meta.index),
        backend.adb_address,
        backend.frida_address,
        status,
        path,
    ]


def _emit_orphan_skip(orphans: list[str]) -> None:
    """
    Print the trailing orphan advisory to stderr so it never pollutes JSON output.
    """
    names = ", ".join(orphans)
    console.note(
        f"(skipping {len(orphans)} orphan "
        f"{'entry' if len(orphans) == 1 else 'entries'}: {names}; "
        f"clean up with 'beetroot destroy <name> -y')"
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
    name: str,
    meta: registry.InstanceMeta,
    backend: api.DeviceBackend,
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
    # row["serial"] can distinguish this row from a redroid instance. The
    # vm-kind backend reaches here too (it has no serial → the None branch).
    serial = getattr(meta.backend, "serial", None)
    if serial is not None:
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
    """
    Tail container logs for an instance.
    """
    _ensure_exists(name)
    backend = api.Manager.resolve(name)
    inst = cast(api.LogReader, _require(backend, api.LogReader, "logs"))
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
            console.out(f"{check_name}: pass", style="green")
        else:
            reason = f" {result.reason}" if result.reason else ""
            console.out(
                f"{check_name}: {result.status}{reason}",
                style="red" if result.status == "fail" else "yellow",
            )
        if result.status == "fail":
            fail_count += 1
    if fail_count > 0:
        # POSIX exit codes top out at 255; clamp so a hypothetical
        # 300-check fan-out doesn't wrap to 44.
        raise typer.Exit(code=min(fail_count, 255))


@app.command()
def modes(
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the support matrix as JSON instead of a table."),
    ] = False,
) -> None:
    """
    Survey the host and report which Beetroot run-modes it supports.

    Host-level and instance-independent — answers "what can this machine
    run before I create anything or pick a `binder` mode?", unlike
    `beetroot doctor <name>` (which health-checks one existing instance).

    Probes the host binder driver, KVM, and the QEMU / Docker / adb
    binaries, then reports each mode as `supported` / `needs-setup` /
    `unsupported` / `unknown` with a reason and remedy. See
    `docs/how-it-works/binder-and-modes.md` for what each mode needs.

    Always exits 0 — it reports, it does not gate.
    """
    results = capabilities.survey()
    if json_out:
        # Plain JSON to stdout (not through rich) so jq/pipelines get clean output.
        print(  # noqa: T201  # plain JSON stdout — must not go through rich
            json.dumps([r.model_dump() for r in results], indent=2, sort_keys=True)
        )
        return
    console.table(
        ("MODE", "STATUS", "DETAIL"),
        [(r.mode, r.status, r.remedy or r.reason) for r in results],
    )


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


def _echo_module_rows(results: list[api.ModuleInstallResult]) -> None:
    for r in results:
        if r.ok:
            console.status(f"ok: {r.source} — {r.detail}")
        else:
            console.note(f"failed: {r.source} — {r.detail}")


def _module_auto_install(
    backend: api.DeviceBackend,
    sources: list[str],
    digests: list[str],
) -> None:
    """
    Drive the ``module --auto-install`` path and report per-module outcomes.

    Each module gets its own ``ok:`` (stdout) or ``failed:`` (stderr)
    line; a failed module never aborts reporting of the rest, and any
    failure makes the verb exit 1. Whole-device problems (offline, no
    usable root, no magisk binary) surface as a single friendly
    ``error: ...`` line + exit 1 via the backend's pre-flight probe
    (issue #38) — any rows completed before a mid-batch abort are still
    reported first.
    """
    installer = cast(
        api.AutoModuleInstaller,
        _require(backend, api.AutoModuleInstaller, "module --auto-install"),
    )
    if digests and len(digests) != len(sources):
        raise _error(
            "--sha256 must be repeated once per source (or omitted entirely) with --auto-install."
        )
    try:
        results = installer.auto_install_modules(sources, sha256s=digests or None)
    except api.AdbNotInstalledError as e:
        raise _error(str(e)) from e
    except api.DevicePreflightError as e:
        _echo_module_rows(e.results)
        raise _error(str(e)) from e
    _echo_module_rows(results)
    if not all(r.ok for r in results):
        raise typer.Exit(code=1)


@app.command()
def module(
    name: Annotated[str, typer.Argument(help="Instance name.")],
    sources: Annotated[
        list[str],
        typer.Argument(
            metavar="SOURCE...",
            help=(
                "Module zip: https URL or instance-relative path (redroid); "
                "existing local .zip path, relative to the CWD (adb). "
                "Multiple sources are allowed with --auto-install only."
            ),
        ),
    ],
    sha256: Annotated[
        list[str] | None,
        typer.Option(
            "--sha256",
            metavar="HEX",
            help=(
                "Expected sha256 hex digest of the zip. With --auto-install, "
                "repeat once per source; a mismatching zip is never pushed."
            ),
        ),
    ] = None,
    auto_install: Annotated[
        bool,
        typer.Option(
            "--auto-install",
            help=(
                "Install via root on an adb-adopted device "
                "(su -c magisk --install-module) instead of the safe "
                "push-to-Downloads default; --sha256 (if given) is "
                "enforced fail-closed."
            ),
        ),
    ] = False,
) -> None:
    """
    Install a Magisk module — append + re-stage (redroid), push (adb), or root --auto-install.
    """
    _ensure_exists(name)
    backend = api.Manager.resolve(name)
    digests = sha256 or []
    if auto_install:
        _module_auto_install(backend, sources, digests)
        return
    if len(sources) != 1 or len(digests) > 1:
        raise _error("without --auto-install, pass exactly one source (and at most one --sha256).")
    installer = cast(api.ModuleInstaller, _require(backend, api.ModuleInstaller, "module"))
    installer.add_module(sources[0], sha256=digests[0] if digests else None)
    if isinstance(backend, api.Instance):
        console.status(f"added module → {paths.instance_yaml(backend.root)}")
        console.hint(f"restart to flash: beetroot down {name} && beetroot up {name}")
    else:
        console.status(f"module pushed to {name}")


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
def build(  # noqa: PLR0913  # Typer verb: each parameter is a distinct user-facing CLI flag
    gapps: Annotated[
        _GappsVariant,
        typer.Argument(help="GMS variant to bake into the base image."),
    ] = _GappsVariant.lite,
    vm_kernel: Annotated[
        bool,
        typer.Option(
            "--vm-kernel",
            help=(
                "Build the binder: vm micro-VM guest kernel + rootfs instead "
                "of the redroid base image (for hosts with no kernel binder)."
            ),
        ),
    ] = False,
    from_source: Annotated[
        bool,
        typer.Option(
            "--from-source",
            help=(
                "With --vm-kernel: always compile the guest kernel from source "
                "instead of fetching the matching prebuilt bzImage (~7 min)."
            ),
        ),
    ] = False,
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help=(
                "With --vm-kernel: only run the host-prerequisite preflight "
                "(busybox/socat/iptables/curl/tar/mke2fs/Docker) and report what's "
                "missing — don't build. Exits 0 when the host is ready, 1 otherwise."
            ),
        ),
    ] = False,
    build_context: Annotated[
        Path | None,
        typer.Option(
            "--build-context",
            help=(
                "Path to a source checkout whose docker/ tree supplies the "
                "build assets. Overrides BEETROOT_BUILD_CONTEXT. Defaults to "
                "the assets bundled in the installed wheel."
            ),
        ),
    ] = None,
    android_version: Annotated[
        int,
        typer.Option(
            "--android-version",
            help=(
                "With --vm-kernel: Android major version to bake into the guest "
                "rootfs (must match the instance's android.version). Defaults to "
                "the shared default so an unflagged build matches a default "
                "`beetroot create`."
            ),
        ),
    ] = config.DEFAULT_ANDROID_VERSION,
) -> None:
    """
    Build the redroid base image, or (with --vm-kernel) the micro-VM artifacts.
    """
    if check and not vm_kernel:
        raise _error("--check only applies to --vm-kernel.")
    if vm_kernel:
        try:
            config.validate_android_version(android_version)
        except ValueError as e:
            raise _error(f"--android-version: {e}") from e
        # Preflight: enumerate every missing host prerequisite in one pass
        # (issue #78), so a bare host isn't a five-failures-deep guessing game.
        _tar = os.environ.get("REDROID_TAR")
        problems = builder.vm_build_preflight(redroid_tar=Path(_tar) if _tar else None)
        if check:
            for problem in problems:
                console.warn(f"{problem.requirement}: {problem.detail} → fix: {problem.fix}")
            if problems:
                raise _error(
                    f"vm build preflight: {len(problems)} host prerequisite(s) missing (see above)."
                )
            console.status("vm build preflight: all host prerequisites satisfied")
            return
        if problems:
            detail = "; ".join(f"{p.requirement} → {p.fix}" for p in problems)
            raise _error(
                f"vm build preflight found {len(problems)} missing host prerequisite(s): "
                f"{detail}. Install them, then re-run `beetroot build --vm-kernel`."
            )
        try:
            artifacts = builder.build_vm_kernel(
                android_version=android_version,
                from_source=from_source,
                build_context=build_context,
            )
        except builder.BootstrapError as e:
            raise _error(str(e)) from e
        console.status(f"micro-VM kernel built: {artifacts.kernel}")
        console.status(f"micro-VM rootfs built: {artifacts.rootfs}")
        console.hint(
            "next: point vm.kernel / vm.rootfs (or BEETROOT_VM_KERNEL "
            "/ BEETROOT_VM_ROOTFS) at these paths and set binder: vm."
        )
        return
    tag = builder.build_image(gapps=gapps.value, build_context=build_context)
    console.status(f"base image built: {tag}")


@app.command(name="snapshot")
def snapshot(
    name: Annotated[str, typer.Argument(help="Instance name to snapshot.")],
    output: Annotated[
        Path | None,
        typer.Option("-o", "--output", help="Archive path (default: ./<name>.tar.zst)."),
    ] = None,
) -> None:
    """
    Pack an instance's host-side state into a .tar.zst archive.
    """
    _ensure_exists(name)
    backend = api.Manager.resolve(name)
    if not isinstance(backend, api.Snapshottable):
        # A registered non-redroid instance (vm / adb) gets the specific
        # #128 message instead of the generic capability one. Still a
        # BackendCapabilityError so the exit code stays 2 (the documented
        # "verb doesn't apply to this backend" code), and the adb case is
        # caught here because an adb row carries no on-disk path for
        # snapshot.snapshot()'s lookup to match against.
        raise api.BackendCapabilityError(
            snapshot_mod.unsupported_backend_message("snapshot", name, backend.kind)
        )
    snappable = cast(api.Snapshottable, backend)
    dest = output if output is not None else Path(f"{name}.tar.zst")
    console.step(f"packing {name} → {dest}")
    try:
        final = snappable.snapshot(dest)
    except snapshot_mod.SnapshotError as e:
        raise _error(str(e)) from e
    console.status(f"snapshot of {name} → {final}")


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
    """
    Unpack a snapshot archive into a new instance and register it.
    """
    # --as is a one-release hidden back-compat alias for --name.
    # If both are given, --name wins (--as is for migration only).
    effective_name = name if name is not None else as_
    try:
        manifest = snapshot_mod.read_manifest(archive)
    except snapshot_mod.SnapshotError as e:
        raise _error(str(e)) from e
    dest_name = effective_name if effective_name is not None else manifest.name
    dest_path = (path if path is not None else Path(dest_name)).resolve()
    console.step(f"unpacking {archive} → {dest_path}")
    try:
        restored = snapshot_mod.restore(
            archive,
            dest_name=dest_name,
            dest_path=dest_path,
            force=force,
        )
    except snapshot_mod.SnapshotError as e:
        raise _error(str(e)) from e
    inst = _load(dest_name)
    p = inst.ports
    console.status(
        f"restored {dest_name} at {restored} "
        f"(index {inst.index}, ADB localhost:{p['adb']}, Frida localhost:{p['frida']})"
    )
    # Agent A's fix made ``snapshot.restore`` call ``_stage()``
    # itself, so an intermediate ``beetroot apply`` is no longer
    # required before ``beetroot up``. CR #3 finding 10.
    console.hint(f"next: beetroot up {dest_name}")


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
        console.error(str(e))
        sys.exit(2)
    except api.InstanceNotFoundError as e:
        # Manager.resolve raises InstanceNotFoundError for unknown names
        # and for unresolvable backend kinds (e.g. package not installed).
        # v0.4 let these propagate as tracebacks; v0.6 catches them for
        # a friendly error: ... line + exit 1.
        console.error(str(e))
        sys.exit(1)
    except paths.InstanceRootNotFoundError as e:
        console.error(str(e))
        sys.exit(1)
    except ports.PortCollisionError as e:
        console.error(str(e))
        sys.exit(1)
    except compose.ComposeError as e:
        console.error(str(e))
        sys.exit(1)
    except vm_qemu.QemuLaunchError as e:
        # Any non-``up`` path to a QEMU launch (e.g. ``restart``, which
        # calls ``up()`` after ``down()``) would otherwise dump a raw
        # traceback. The ``up`` verb catches this inline for parity with
        # its banner; this net covers every other verb so a missing
        # artifact / ``accel: kvm`` without ``/dev/kvm`` maps to the same
        # friendly ``error: ...`` + exit 1.
        console.error(str(e))
        sys.exit(1)
    except builder.BootstrapError as e:
        console.error(str(e))
        sys.exit(1)
    except modules_download.ModuleFetchError as e:
        console.error(str(e))
        sys.exit(1)
    except registry.RegistryError as e:
        # T2 Agent 3 1.9: any code path that walks the registry can
        # surface a RegistryError ("unknown instance X", "X is an
        # adb backend, no on-disk dir") that v0.3 let propagate as
        # a Rich-rendered traceback. Catch it alongside the other
        # domain exceptions for a friendly ``error: ...`` line.
        console.error(str(e))
        sys.exit(1)
    except (pydantic.ValidationError, yaml.YAMLError) as e:
        # A hostile or corrupt ``beetroot.yaml`` (wrong field types,
        # unsupported ``api_version``, the renamed ``stealth:`` section,
        # or malformed YAML syntax) reaches ``config.load_yaml`` deep in
        # the call tree. ``register``/``adopt`` catch ``ValueError`` (and
        # ``ValidationError`` subclasses it) inline, but every name-resolved
        # verb (``status``, ``up``, ``apply``, …) let these propagate as a
        # Rich-rendered traceback. ``yaml.YAMLError`` isn't a ``ValueError``
        # at all, so even ``register`` tracebacked on a syntactically broken
        # file. Catch both here for the uniform ``error: ...`` + exit 1
        # contract the rest of the CLI upholds.
        console.error(str(e))
        sys.exit(1)
    except FileNotFoundError as e:
        # Belt-and-suspenders: an instance whose on-disk dir was
        # ``rm -rf``'d behind the CLI's back leaves a stale registry
        # entry; ``Instance.load`` then trips on the missing
        # ``beetroot.yaml`` deep in the call tree. ``Manager.list``
        # filters orphans itself, but a verb that targets the orphan
        # by name still needs this safety net.
        console.error(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()

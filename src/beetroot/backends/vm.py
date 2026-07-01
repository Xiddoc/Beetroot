"""
``VmDeviceBackend`` — redroid inside an emulated QEMU micro-VM (``binder: vm``).

This is the engine behind the ``binder: vm`` capability ladder rung from
``docs/design/binderless-hosts-qemu-tcg.md``: on a host with no kernel
binder driver, Beetroot boots a Beetroot-built guest kernel (binder
compiled in) under QEMU, which auto-starts redroid inside Docker. The host
reaches the guest's ``adbd`` through a user-net ``hostfwd`` port mapping
(guest 5555 → the instance's stride-allocated host ADB port).

The backend is **directory-backed** (like the redroid :class:`Instance`):
the instance directory holds ``beetroot.yaml`` (carrying the ``vm:``
tunables and the optional ``ports:`` overrides) plus the QEMU pidfile
written at ``up`` time. Lifecycle (``up`` / ``down`` / ``restart``) drives
:class:`beetroot.vm.qemu.QemuProcess`; the device operations (``shell`` /
``frida_cli`` / ``install_frida``) mirror :class:`AdbDevice`'s
adb-forward-over-loopback patterns since, once booted, the guest's redroid
is just another adb-reachable rooted Android.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Self

from beetroot import builder, capabilities, config, console, paths, ports, registry
from beetroot.api import (
    FRIDA_ADDRESS_UNSUPPORTED,
    AdbNotInstalledError,
    BackendCapabilityError,
    InstanceNotFoundError,
    _check_adb_connect,
)
from beetroot.backends import register_backend
from beetroot.settings import settings
from beetroot.vm import boot_cache, qemu

if TYPE_CHECKING:
    from beetroot.api import CheckResult

_ADB = "adb"

# Sidecar the warm-resume console trace is copied to before a warm→cold-boot
# fallback: the cold retry's `proc.start` truncates the live console log the
# QemuLaunchError pointed the user at, so the warm-failure trace is preserved
# here for post-mortem (issue #267).
_WARM_FAIL_CONSOLE_SUFFIX = ".warm-fail"

# How often ``up`` re-tries ``adb connect`` against the freshly-launched
# guest, and the per-attempt subprocess timeout. The deadline itself is the
# configurable ``settings.vm_adb_connect_timeout`` — the guest restarts adbd
# to enable TCP a few seconds *after* ``sys.boot_completed=1``, so the first
# connect almost always races that late bind and must be retried.
_ADB_CONNECT_POLL_SECONDS = 1.0
_ADB_CONNECT_ATTEMPT_TIMEOUT = 5

# The boot-cache path must checkpoint a *fully booted* guest, so it gates
# ``savevm`` on a real ``getprop sys.boot_completed == 1`` poll. That is a
# strictly stronger guarantee than ``_wait_for_adb_connect``: the latter only
# proves the in-guest relay accepts an adb attach, while this confirms Android
# itself reached ``sys.boot_completed`` (guest-init.sh main() starts the relay
# only *after* ``wait_for_boot``, so an accepted connect implies boot, but a
# checkpoint wants the prop read to be certain). The deadline matches
# guest-init.sh's own BOOT_TIMEOUT; a cold TCG boot is minutes.
_BOOT_COMPLETED_TIMEOUT_SECONDS = 900
_BOOT_COMPLETED_POLL_SECONDS = 3.0
_BOOT_COMPLETED_ATTEMPT_TIMEOUT = 10

# Floor for the accel-aware ADB-connect deadline under TCG. A cold TCG boot to
# first host ADB is minutes (~222 s for Android 14, vm-rnd-log Stage E), so the
# flat ``settings.vm_adb_connect_timeout`` KVM default (60 s) aborts ``up`` long
# before the guest's relay binds. Under TCG the deadline is raised to a
# boot-completed-scale floor so a slow first boot is waited out, not failed
# (issue #160). KVM keeps the short, configurable default.
_TCG_ADB_CONNECT_FLOOR_SECONDS = _BOOT_COMPLETED_TIMEOUT_SECONDS

# Frida is not yet wired through the QEMU micro-VM (issue #44 scopes the
# vm backend to ADB forwarding only): the guest runs redroid with
# ``--network none`` and nothing forwards the guest Frida port or
# bind-mounts the staged frida-server into the guest. Surfacing a working
# endpoint would be a lie, so the frida verbs fail loudly with this
# pointer rather than silently no-op.
_FRIDA_UNSUPPORTED = (
    "Frida is not yet supported on the 'vm' backend: the QEMU micro-VM "
    "guest is network-isolated, so neither the staged frida-server nor "
    "the guest Frida port is reachable from the host. Track the follow-up "
    "at https://github.com/Xiddoc/Beetroot/issues/44 (Frida-over-VM "
    "forwarding). Use `binder: auto`/`host` (redroid) or `beetroot adopt` "
    "an external rooted device for Frida in the meantime."
)


def _resolve_artifact(configured: str | None, env_default: str, label: str) -> Path:
    """
    Resolve a VM artifact path from the config value, falling back to the env default.

    Args:
        configured: The ``vm.kernel`` / ``vm.rootfs`` value from the config
            (``None`` when unset).
        env_default: The ``BEETROOT_VM_*`` settings default (``""`` when
            unset).
        label: Human label for the error message (``"kernel"`` / ``"rootfs"``).

    Returns:
        The resolved artifact path.

    Raises:
        qemu.QemuLaunchError: If neither the config nor the env supplies a
            path, or if the resolved path does not exist on the host.
    """
    raw = configured or env_default or None
    if raw is None:
        raise qemu.QemuLaunchError(
            f"no VM {label} configured — set vm.{label} in beetroot.yaml or "
            f"the BEETROOT_VM_{label.upper()} environment variable. Build one "
            f"with `beetroot build --vm-kernel`."
        )
    # Expand a leading ``~`` — the shipped examples/vm.yaml points kernel/rootfs
    # at ``~/.cache/beetroot/vm/...`` (where ``beetroot build --vm-kernel``
    # writes them), and YAML carries the tilde literally. Without this the
    # documented example config never boots ("does not exist on the host").
    path = Path(raw).expanduser()
    if not path.exists():
        raise qemu.QemuLaunchError(
            f"VM {label} {raw!r} does not exist on the host filesystem. "
            f"Build it with `beetroot build --vm-kernel`."
        )
    # Resolve to an absolute path so the artifact is cwd-independent. A relative
    # ``vm.rootfs`` is later handed to ``qemu-img create -b`` as the qcow2
    # overlay's backing file, and qemu records that reference *relative to the
    # overlay's directory* (the instance dir, not the process cwd). Without
    # resolving, the existence check (run against cwd) could pass while the
    # stored backing reference points elsewhere, leaving the overlay unopenable.
    return path.resolve()


def _check_port_collisions(name: str, new_ports: list[ports.ResolvedPort]) -> None:
    """
    Raise ``ValueError`` if ``new_ports`` collide with any other instance.

    Mirrors ``beetroot.api._check_port_collisions`` (kept local to avoid
    reaching into the api module's private surface) so a ``beetroot apply``
    on a VM instance catches a port clash the same way the redroid backend
    does.

    Args:
        name: This instance's registry name (excluded from the comparison).
        new_ports: The resolved port list to validate.

    Raises:
        ValueError: On the first colliding port.
    """
    others = {n: p for n, p in registry.all_resolved_host_ports().items() if n != name}
    collision = registry.find_port_collision(new_ports, others)
    if collision is None:
        return
    port, other_name, port_kind = collision
    raise ValueError(
        f"port {port} ({port_kind}) collides with instance {other_name!r} "
        f"(which also uses {port}). Pin or remove one."
    )


class VmDeviceBackend:
    """
    Backend that boots redroid inside a QEMU micro-VM with its own binder kernel.

    Attributes:
        _name: Registry name for this backend.
        _root: Absolute path to the instance directory.
        _cfg: The parsed ``beetroot.yaml`` for this instance.
        _index: The stride-of-10 port index allocated to this instance.
    """

    def __init__(
        self,
        name: str,
        root: Path,
        cfg: config.InstanceConfig,
        index: int,
    ) -> None:
        """
        Bind a name + on-disk root + parsed config + port index.

        Most callers use :meth:`from_meta`.

        Args:
            name: Registry name for this backend.
            root: Absolute path to the instance directory.
            cfg: Parsed instance configuration.
            index: The stride-of-10 port index for this instance.
        """
        self._name = name
        self._root = root
        self._cfg = cfg
        self._index = index

    @classmethod
    def from_meta(
        cls,
        name: str,
        backend: registry.BackendConfigBase,
    ) -> Self:
        """
        Build a :class:`VmDeviceBackend` from a registry meta's backend config.

        Args:
            name: Registry name.
            backend: The matching backend config. Must be a
                :class:`~beetroot.registry.VmBackendConfig`.

        Returns:
            The hydrated :class:`VmDeviceBackend`.

        Raises:
            beetroot.api.InstanceNotFoundError: If ``backend`` is not a
                :class:`~beetroot.registry.VmBackendConfig`, if the registry
                row is missing, or if the ``beetroot.yaml`` is gone (orphan).
        """
        if not isinstance(backend, registry.VmBackendConfig):
            raise InstanceNotFoundError(
                f"VmDeviceBackend expected VmBackendConfig, got {type(backend).__name__}",
            )
        meta = registry.get(name)
        if meta is None:
            raise InstanceNotFoundError(
                f"no instance named {name!r} in registry; cannot derive ports",
            )
        root = Path(backend.absolute_path)
        try:
            cfg = config.load_yaml(paths.instance_yaml(root))
        except FileNotFoundError as exc:
            raise InstanceNotFoundError(
                f"instance {name!r} has no beetroot.yaml at {root}; "
                f"it may be an orphan — run `beetroot destroy {name}` to clean up"
            ) from exc
        return cls(name=name, root=root, cfg=cfg, index=meta.index)

    # ---- DeviceBackend Protocol surface -----------------------------------

    @property
    def name(self) -> str:
        """
        Registry name for this backend.
        """
        return self._name

    @property
    def kind(self) -> Literal["vm"]:
        """
        Backend discriminator — always ``"vm"``.
        """
        return "vm"

    @property
    def root(self) -> Path:
        """
        Absolute path to the instance directory.
        """
        return self._root

    @property
    def config(self) -> config.InstanceConfig:
        """
        The parsed ``beetroot.yaml`` for this instance.
        """
        return self._cfg

    @property
    def ports(self) -> list[ports.ResolvedPort]:
        """
        Resolved guest→host port mappings for this instance (full list).

        Use :func:`beetroot.ports.well_known` to project to the
        ``{service: host}`` dict the address accessors key off.
        """
        return ports.resolve_ports(self._index, self._cfg.ports)

    @property
    def adb_address(self) -> str:
        """
        ``localhost:<adb_port>`` — the QEMU-forwarded guest adbd port.
        """
        return f"localhost:{ports.well_known(self.ports)['adb']}"

    @property
    def frida_address(self) -> str:
        """
        Report Frida as unsupported on the vm backend.

        Frida-over-VM is not yet wired through the network-isolated guest
        (issue #44), so this never names a reachable endpoint — it returns
        the :data:`~beetroot.api.FRIDA_ADDRESS_UNSUPPORTED` sentinel so
        ``ls`` / ``status`` rows don't advertise a working
        ``localhost:<port>`` that Frida could never connect to. ``frida_cli``
        and the ``frida-addr`` verb turn this into a loud
        :class:`~beetroot.api.BackendCapabilityError` rather than emitting it.
        """
        return FRIDA_ADDRESS_UNSUPPORTED

    @property
    def is_available(self) -> bool:
        """
        True iff the QEMU process for this instance is alive.
        """
        return self._qemu().is_running()

    def install_frida(self, version: str | None = None) -> None:
        """
        Reject Frida installation — unsupported on the QEMU micro-VM backend.

        The network-isolated guest can neither read a staged frida-server
        nor expose its Frida port to the host (issue #44 scopes the vm
        backend to ADB forwarding only), so staging a binary would be a
        no-op that lies about working Frida. Raise loudly instead.

        Args:
            version: Ignored — the call always raises.

        Raises:
            BackendCapabilityError: Always — Frida is unsupported on ``vm``.
        """
        del version
        raise BackendCapabilityError(_FRIDA_UNSUPPORTED)

    def shell(self, args: Sequence[str] | None = None) -> int:
        """
        Open an ADB shell into the guest via the forwarded loopback port.

        Mirrors :meth:`beetroot.api.Instance.shell`: ``adb connect`` to the
        forwarded address first (the VM exposes adbd on a host loopback
        port), then ``adb -s <addr> shell``.

        Args:
            args: Optional extra tokens appended after ``adb -s <addr>
                shell``. ``None`` opens an interactive shell.

        Returns:
            The exit code of the ``adb shell`` invocation.

        Raises:
            AdbNotInstalledError: If the ``adb`` binary is not on PATH.
        """
        if shutil.which(_ADB) is None:
            raise AdbNotInstalledError("adb not found on PATH (install android-tools)")
        target = self.adb_address
        subprocess.run([_ADB, "connect", target], check=False)  # noqa: S603  # adb is a host CLI resolved via PATH; target is localhost:<port>
        cmd = [_ADB, "-s", target, "shell", *(args or [])]
        res = subprocess.run(cmd, check=False)  # noqa: S603  # same as above
        return int(res.returncode)

    def logs(self, *, follow: bool = False) -> None:
        """
        Surface the guest's persisted QEMU serial console.

        Unlike the redroid backend (which tails ``docker compose logs``),
        the micro-VM has no Docker project on the host — its only log is the
        guest serial console that
        :meth:`beetroot.vm.qemu.QemuProcess.start` redirects to
        ``<instance>/qemu-console.log``. This prints that file (the kernel
        boot trace, ``guest-init`` output, and the in-guest redroid
        container's stdout). With ``follow`` it streams via ``tail -f``
        (Ctrl-C to stop), mirroring ``docker compose logs -f``.

        Args:
            follow: If True, stream continuously with ``tail -f`` instead of
                printing the current contents once.
        """
        log = self._qemu().console_log
        if not log.is_file():
            console.warn(
                f"no QEMU console log for {self._name!r} yet at {log} — "
                "has the VM been started with `beetroot up`?"
            )
            return
        if follow:
            subprocess.run(  # noqa: S603  # tail is a host coreutils CLI; the only argument is a repo-controlled path
                ["tail", "-n", "+1", "-f", str(log)],  # noqa: S607  # tail resolved via PATH, matching the rest of the host-CLI calls here
                check=False,
            )
            return
        sys.stdout.write(log.read_text(errors="replace"))

    def frida_cli(self, args: Sequence[str]) -> int:
        """
        Reject the ``frida`` CLI — unsupported on the QEMU micro-VM backend.

        The network-isolated guest never exposes its Frida port to the
        host (issue #44 scopes the vm backend to ADB forwarding only), so
        ``frida -H localhost:<port>`` could never connect. Raise loudly
        rather than spawn a ``frida`` that hangs against a dead port.

        Args:
            args: Ignored — the call always raises.

        Raises:
            BackendCapabilityError: Always — Frida is unsupported on ``vm``.
        """
        del args
        raise BackendCapabilityError(_FRIDA_UNSUPPORTED)

    # ---- Lifecycle capability ---------------------------------------------

    def resolved_accel(self) -> qemu.ResolvedAccel:
        """
        Resolve this instance's configured accelerator against the host probe.

        Returns:
            ``"kvm"`` or ``"tcg"`` — the accelerator QEMU will use.

        Raises:
            qemu.QemuLaunchError: If ``vm.accel: kvm`` was requested but
                ``/dev/kvm`` is unavailable.
        """
        return qemu.detect_accel(self._cfg.vm.accel)

    def _qemu(self) -> qemu.QemuProcess:
        """
        Return this instance's QEMU process manager, keyed by its host ADB port.

        The host ADB port is the per-instance identity token
        :meth:`qemu.QemuProcess._pid_is_qemu` matches against (#162), so every
        construction site must supply it.
        """
        return qemu.QemuProcess(self._root, ports.well_known(self.ports)["adb"])

    def build_argv(self, accel: qemu.ResolvedAccel) -> list[str]:
        """
        Build the QEMU argv for this instance under the resolved accelerator.

        Args:
            accel: The resolved accelerator (from :meth:`resolved_accel`).

        Returns:
            The full QEMU argv (see :func:`beetroot.vm.qemu.build_qemu_argv`).

        Raises:
            qemu.QemuLaunchError: If the kernel / rootfs artifact is missing.
        """
        kernel = _resolve_artifact(self._cfg.vm.kernel, settings.vm_kernel, "kernel")
        rootfs = _resolve_artifact(self._cfg.vm.rootfs, settings.vm_rootfs, "rootfs")
        return qemu.build_qemu_argv(
            qemu_bin=settings.qemu_bin,
            accel=accel,
            kernel=kernel,
            rootfs=rootfs,
            smp=qemu.resolve_smp(self._cfg.vm.smp),
            memory_mib=self._cfg.vm.memory_mib,
            host_adb_port=ports.well_known(self.ports)["adb"],
        )

    def up(self) -> None:
        """
        Boot the micro-VM: resolve accel, build argv, launch QEMU, attach adb.

        Re-checks ``cfg.binder == "vm"`` first: a vm-registry row whose
        ``beetroot.yaml`` was hand-edited back to ``binder: host``/``auto``
        would otherwise boot QEMU anyway, contradicting the on-disk intent.
        Fail fast with a ``beetroot apply`` pointer instead (mirrors the
        up-verb's redroid-row-but-yaml-vm guard).

        After QEMU is launched, ``adb connect`` is retried with backoff
        (:meth:`_wait_for_adb_connect`): the guest restarts adbd to enable
        TCP a few seconds *after* ``sys.boot_completed=1``, so a single
        connect right after launch races that late bind and fails.

        Raises:
            BackendCapabilityError: If the on-disk config no longer sets
                ``binder: vm`` (the row is out of sync — run ``apply``).
            qemu.QemuLaunchError: On a missing accelerator, missing
                kernel/rootfs, a launch failure, or if the guest does not
                expose ADB within the accel-aware deadline (long under TCG,
                ``settings.vm_adb_connect_timeout`` under KVM). On timeout the
                just-launched QEMU is terminated so the next ``up`` starts
                clean (issue #174).
        """
        if self._cfg.binder != "vm":
            raise BackendCapabilityError(
                f"instance {self._name!r} is registered as a vm backend but its "
                f"beetroot.yaml now sets binder: {self._cfg.binder!r}. The two are "
                f"out of sync — run `beetroot apply {self._name}` to reconcile the "
                "registry before `beetroot up`."
            )
        self._warn_on_rootfs_version_skew()
        accel = self.resolved_accel()
        if self._cfg.vm.boot_cache:
            self._up_cached(accel)
            return
        argv = self.build_argv(accel)
        proc = self._qemu()
        proc.start(argv)
        try:
            self._wait_for_adb_connect(accel, proc)
        except BaseException:
            # A timed-out (or otherwise failed) wait must not orphan the QEMU we
            # just launched — its live pidfile would trip start()'s
            # already-running guard on the next `up` (issue #174).
            self.down()
            raise

    def _up_cached(self, accel: qemu.ResolvedAccel) -> None:  # noqa: PLR0915  # one cohesive boot-cache orchestration sharing the _launch closure + many locals
        """
        Boot via the warm-start boot cache: resume a checkpoint, or cold-boot + checkpoint.

        On the first ``up`` the qcow2 overlay carries no snapshot, so QEMU
        cold-boots (through the overlay, with an HMP monitor socket) and — once
        ADB is reachable — :func:`boot_cache.save_snapshot` checkpoints the
        running machine state. Every later ``up`` finds the snapshot and
        launches with ``-loadvm``, resuming the booted guest in ~10 s instead
        of cold-booting in minutes under TCG (issue #49/#83).

        The checkpoint is best-effort: if ``savevm`` fails the VM still runs
        (the next ``up`` just cold-boots again), so a failed checkpoint is a
        warning, never a hard error.

        A warm ``-loadvm`` resume that dies on an unrestorable snapshot is
        re-checked for liveness during the ADB wait (issue #176): rather than
        burn the full deadline with a misleading TCG-slowness error, the soured
        overlay is discarded and the boot retried **exactly once** as a cold
        boot.

        Args:
            accel: The resolved accelerator (from :meth:`resolved_accel`).

        Raises:
            qemu.QemuLaunchError: On a missing kernel/rootfs, a missing
                ``qemu-img``, a launch failure, or if the guest does not expose
                ADB within the accel-aware deadline.
        """
        kernel = _resolve_artifact(self._cfg.vm.kernel, settings.vm_kernel, "kernel")
        base_rootfs = _resolve_artifact(self._cfg.vm.rootfs, settings.vm_rootfs, "rootfs")
        overlay = boot_cache.overlay_path(self._root)
        monitor = boot_cache.monitor_path(self._root)
        # Resolve the -smp/-m geometry once: it feeds the staleness fingerprint
        # (#161 — a geometry edit must invalidate the checkpoint, since QEMU
        # rejects a -loadvm into a mismatched geometry), the identity sidecar,
        # and the launch argv, which must all agree.
        resolved_smp = qemu.resolve_smp(self._cfg.vm.smp)
        memory_mib = self._cfg.vm.memory_mib
        # A stale monitor socket from a prior `down` would block QEMU's bind.
        monitor.unlink(missing_ok=True)
        # Auto-invalidate a checkpoint taken against now-changed kernel/rootfs or
        # -smp/-m geometry (#126/#161): resuming a snapshot booted from stale
        # artifacts is worse than one cold boot. An overlay with no recorded
        # identity (pre-#126) also counts as stale, so it is re-keyed next boot.
        if overlay.exists() and boot_cache.overlay_is_stale(
            self._root, kernel, base_rootfs, resolved_smp, memory_mib, accel
        ):
            console.note(
                f"{self._name!r} boot-cache overlay was built from a different "
                "kernel/rootfs/geometry/accelerator; discarding the stale checkpoint "
                "and cold-booting once to re-cache."
            )
            boot_cache.discard_overlay(self._root)
        elif overlay.exists() and not boot_cache.snapshot_present(overlay):
            # The overlay is identity-fresh but carries no snapshot: an aborted
            # first cold boot left a dirty COW layer (partial guest writes, no
            # checkpoint). Reusing it would cold-boot over soured state, so
            # discard it and start the cold boot on a pristine overlay (#175).
            boot_cache.discard_overlay(self._root)
        if not overlay.exists():
            boot_cache.create_overlay(base_rootfs, overlay)
            # Record what this overlay was built from so a later rebuild invalidates it.
            boot_cache.record_identity(
                self._root, kernel, base_rootfs, resolved_smp, memory_mib, accel
            )
        warm = boot_cache.snapshot_present(overlay)
        if warm:
            console.info(f"resuming cached boot snapshot for {self._name!r} (warm start)")
            self._warn_on_boot_cache_data_revert()
        else:
            console.info(
                f"no boot snapshot yet for {self._name!r}; cold-booting once, then caching"
            )

        def _launch(*, loadvm: str | None) -> None:
            argv = qemu.build_qemu_argv(
                qemu_bin=settings.qemu_bin,
                accel=accel,
                kernel=kernel,
                rootfs=overlay,
                smp=resolved_smp,
                memory_mib=memory_mib,
                host_adb_port=ports.well_known(self.ports)["adb"],
                disk_format="qcow2",
                monitor_socket=monitor,
                loadvm=loadvm,
            )
            proc = self._qemu()
            proc.start(argv)
            try:
                # Pass proc so a dead-on-arrival -loadvm resume is caught fast
                # (issue #176) instead of burning the full deadline.
                self._wait_for_adb_connect(accel, proc)
            except BaseException:
                # Don't orphan the QEMU we just launched (#174).
                self.down()
                raise

        if warm:
            try:
                _launch(loadvm=boot_cache.SNAPSHOT_TAG)
            except qemu.QemuLaunchError:
                # The warm resume died on an unrestorable snapshot. Fall back to
                # a single cold boot on a fresh overlay rather than failing the
                # `up` outright (#176) — bounded to one retry, no recursion.
                # Preserve the warm-failure console trace first: the cold retry's
                # `proc.start` truncates the console log the QemuLaunchError just
                # pointed the user at, so copy it to a sidecar before it's lost
                # (#267).
                self._preserve_warm_fail_console()
                console.warn(
                    f"warm resume for {self._name!r} failed; discarding the "
                    "snapshot and cold-booting once. See `beetroot logs`."
                )
                boot_cache.discard_overlay(self._root)
                boot_cache.create_overlay(base_rootfs, overlay)
                boot_cache.record_identity(
                    self._root, kernel, base_rootfs, resolved_smp, memory_mib, accel
                )
                warm = False
                _launch(loadvm=None)
            else:
                # Resume restores an already-booted guest; nothing to checkpoint.
                return
        else:
            _launch(loadvm=None)

        # Cold boot: the checkpoint must capture a FULLY booted guest, so gate
        # on a real boot_completed poll — a strictly stronger guarantee than the
        # adb-connect attach (the relay accepts a connect only once Android has
        # booted, but the prop read makes the checkpoint precondition explicit).
        if not self._wait_for_boot_completed():
            console.warn(
                f"guest {self._name!r} did not reach sys.boot_completed in "
                f"{_BOOT_COMPLETED_TIMEOUT_SECONDS}s; not checkpointing (the next "
                "`up` will cold-boot again). See `beetroot logs`."
            )
            return
        if boot_cache.save_snapshot(monitor):
            console.info(f"cached boot snapshot for {self._name!r}; future `up` resumes in seconds")
        else:
            console.warn(
                f"could not checkpoint {self._name!r} (savevm failed); "
                "the next `up` will cold-boot again. See `beetroot logs`."
            )

    def _preserve_warm_fail_console(self) -> None:
        """
        Copy the console log to a ``.warm-fail`` sidecar before a cold-boot retry (#267).

        The warm-resume failure raises a :class:`qemu.QemuLaunchError` whose
        message points the user at the QEMU console log, but the very next cold
        retry's :meth:`qemu.QemuProcess.start` truncates that log — so the trace
        the error names would be gone by the time the user reads it. Snapshot the
        console log to ``<console>.warm-fail`` first so the warm-failure output
        survives the cold retry. Best-effort: a missing console log (QEMU died
        before writing one) is a no-op, matching the boot cache's best-effort
        posture.
        """
        console_log = self._qemu().console_log
        if not console_log.is_file():
            return
        sidecar = console_log.with_name(console_log.name + _WARM_FAIL_CONSOLE_SUFFIX)
        shutil.copy2(console_log, sidecar)

    def _wait_for_boot_completed(self) -> bool:
        """
        Poll ``adb shell getprop sys.boot_completed`` until it reads ``1``.

        Stronger than :meth:`_wait_for_adb_connect` (which is satisfied by the
        in-guest relay accepting an adb-connect attach): this confirms the
        guest's Android actually read ``sys.boot_completed == 1`` — the
        precondition for a useful ``savevm`` checkpoint. Returns ``False`` on
        timeout so the caller can skip the checkpoint rather than snapshot a
        half-booted guest.

        Returns:
            ``True`` once ``sys.boot_completed`` reads ``1``; ``False`` if the
            deadline elapses first.

        Raises:
            AdbNotInstalledError: If the ``adb`` binary is not on PATH.
        """
        if shutil.which(_ADB) is None:
            raise AdbNotInstalledError("adb not found on PATH (install android-tools)")
        target = self.adb_address
        deadline = time.monotonic() + _BOOT_COMPLETED_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._boot_completed(target):
                return True
            time.sleep(_BOOT_COMPLETED_POLL_SECONDS)
        return False

    @staticmethod
    def _boot_completed(target: str) -> bool:
        """
        Return True iff ``getprop sys.boot_completed`` reads ``1`` on ``target``.

        Reconnects first (``adb connect``) since the relay endpoint may have
        only just come up, then reads the prop. Any adb error (endpoint not yet
        accepting, transient timeout) is reported as "not booted yet".

        Args:
            target: The ``host:port`` adb endpoint.

        Returns:
            True only on a clean ``1`` reading.
        """
        try:
            subprocess.run(  # noqa: S603  # adb is a host CLI resolved via PATH; target is localhost:<port>
                [_ADB, "connect", target],
                check=False,
                capture_output=True,
                text=True,
                timeout=_BOOT_COMPLETED_ATTEMPT_TIMEOUT,
            )
            res = subprocess.run(  # noqa: S603  # same as above
                [_ADB, "-s", target, "shell", "getprop", "sys.boot_completed"],
                check=False,
                capture_output=True,
                text=True,
                timeout=_BOOT_COMPLETED_ATTEMPT_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return res.stdout.strip() == "1"

    def _warn_on_rootfs_version_skew(self) -> None:
        """
        Warn (without aborting) if the baked rootfs Android version != config.

        ``beetroot build --vm-kernel`` records the Android version it baked in a
        marker beside the rootfs image (issue #82). If the instance's
        ``android.version`` now disagrees, the guest would boot a different
        Android than the instance expects (e.g. a default-14 instance against a
        rootfs baked for 11 — minSdk>30 APKs would fail to install). Stay silent
        when the versions match, when no marker exists (a pre-#82 rootfs, kept
        for backward compatibility), or when the rootfs path can't be resolved
        (``up`` surfaces that as its own hard error downstream).
        """
        try:
            rootfs = _resolve_artifact(self._cfg.vm.rootfs, settings.vm_rootfs, "rootfs")
        except qemu.QemuLaunchError:
            return
        baked = builder.read_rootfs_version(rootfs)
        configured = self._cfg.android.version
        if baked is None or baked == configured:
            return
        console.note(
            f"warning: instance {self._name!r} sets android.version: {configured} "
            f"but its VM rootfs was baked for Android {baked}. The guest will boot "
            f"Android {baked} — apps targeting a newer minSdk may fail to install. "
            f"Rebuild with `beetroot build --vm-kernel --android-version {configured}` "
            "to match (or set android.version to "
            f"{baked} in beetroot.yaml)."
        )

    def _warn_on_inert_vm_config(self) -> None:
        """
        Print a single apply-time advisory naming every set-but-inert field.

        Delegates both the field→backend applicability matrix and the message
        text to :func:`config.warn_inert_fields` (issue #104) — they live there,
        in code, single-sourced with the redroid ``Instance.apply`` path that
        surfaces the same advisory when a hand-edited ``binder: vm`` config
        first flips the registry kind. The ``binder: vm`` guest boots an
        unmodified upstream redroid image (:func:`config.vm_redroid_image`), so
        the layered-image knobs (``android.gapps``, ``magisk.denylist``) and the
        whole ``frida:`` block are inert; this surfaces them ONCE at ``apply``
        time (no longer on every ``up``) so a researcher who set ``gapps: full``
        isn't left debugging missing Play Services. Non-fatal note, matching
        :meth:`_warn_on_rootfs_version_skew`.
        """
        config.warn_inert_fields(self._cfg, self._name)

    def _warn_on_boot_cache_data_revert(self) -> None:
        """
        Warn that a ``vm.boot_cache`` warm resume reverts ``/data`` to its checkpoint.

        The warm-start ``-loadvm`` resumes the whole machine — RAM, devices, and
        the qcow2 overlay disk that backs the guest's ``/data`` — from the
        first-boot checkpoint, so anything written to ``/data`` since then
        (installed apps, account logins, flashed-module / LSPosed scope state) is
        silently discarded on every ``up``. The behaviour is documented but was
        never surfaced at runtime (issue #123); this advisory makes it visible at
        the point of harm. The remedy is ``vm.boot_cache: false`` — *not*
        ``beetroot snapshot``, which is redroid-only (issue #128). A non-fatal
        note, matching :meth:`_warn_on_inert_vm_config`.

        Suppressed for an ``lifecycle: ephemeral`` instance (issue #124): a
        throwaway phone asked for a reset each boot, so the revert is the
        intended behaviour, not a surprise worth warning about.
        """
        if self._cfg.lifecycle == "ephemeral":
            return
        console.note(
            f"warning: instance {self._name!r} uses vm.boot_cache, so this warm "
            "resume reverts the guest to its first-boot checkpoint — everything "
            "written to /data since then (installed apps, account logins, "
            "flashed-module / LSPosed state) is discarded on every `up`. boot_cache "
            "trades a durable /data for a fast known-good boot; set vm.boot_cache: "
            "false in beetroot.yaml to keep /data across restarts."
        )

    @staticmethod
    def _adb_connect_deadline_seconds(accel: qemu.ResolvedAccel) -> int:
        """
        Resolve the ADB-connect deadline (seconds) for the resolved accelerator.

        ``settings.vm_adb_connect_timeout`` is the KVM (near-native) budget. A
        cold TCG boot to first host ADB is minutes (~222 s for Android 14), so
        under TCG the deadline is raised to a boot-completed-scale floor
        (:data:`_TCG_ADB_CONNECT_FLOOR_SECONDS`) — never *below* the configured
        value, so bumping ``BEETROOT_VM_ADB_CONNECT_TIMEOUT`` still wins (issue
        #160).

        Args:
            accel: The resolved accelerator QEMU launches with.

        Returns:
            The deadline in seconds.
        """
        if accel == "tcg":
            return max(settings.vm_adb_connect_timeout, _TCG_ADB_CONNECT_FLOOR_SECONDS)
        return settings.vm_adb_connect_timeout

    def _wait_for_adb_connect(
        self, accel: qemu.ResolvedAccel, proc: qemu.QemuProcess | None = None
    ) -> None:
        """
        Poll ``adb connect`` against the guest until it accepts or times out.

        The guest forwards adbd to a host loopback port, but redroid restarts
        adbd to switch it into TCP mode a few seconds *after* boot completes,
        so the endpoint refuses connections briefly. Retry ``adb connect``
        every :data:`_ADB_CONNECT_POLL_SECONDS` until the host adb reports a
        successful attach or the accel-aware deadline elapses (long under TCG,
        short under KVM — see :meth:`_adb_connect_deadline_seconds`). The happy
        path (endpoint already up) succeeds on the first attempt and never
        sleeps.

        When ``proc`` is supplied the loop also re-checks QEMU liveness each
        round: a ``-loadvm`` that exits in ~1 s on an unrestorable snapshot is
        otherwise indistinguishable from a slow boot and would burn the full
        deadline. A dead-on-arrival QEMU raises a fast, ``beetroot logs``-pointing
        error instead (issue #176).

        Args:
            accel: The resolved accelerator (selects the deadline).
            proc: The just-launched QEMU handle whose liveness is polled. When
                ``None`` the liveness re-check is skipped (cold-boot path).

        Raises:
            AdbNotInstalledError: If the ``adb`` binary is not on PATH.
            qemu.QemuLaunchError: If QEMU exits before ADB attaches, or if no
                attempt succeeds within the deadline — a friendly, actionable
                message (not a traceback).
        """
        if shutil.which(_ADB) is None:
            raise AdbNotInstalledError("adb not found on PATH (install android-tools)")
        target = self.adb_address
        budget = self._adb_connect_deadline_seconds(accel)
        deadline = time.monotonic() + budget
        while True:
            if self._adb_connect_ok(target):
                return
            if proc is not None and not proc.is_running():
                raise qemu.QemuLaunchError(
                    f"the QEMU micro-VM for {self._name!r} exited before exposing ADB "
                    f"at {target} (a -loadvm resume onto an unrestorable snapshot, or "
                    f"a launch failure). Inspect the guest console with "
                    f"`beetroot logs {self._name}` ({self._qemu().console_log})."
                )
            if time.monotonic() >= deadline:
                raise qemu.QemuLaunchError(
                    f"the QEMU micro-VM for {self._name!r} did not expose ADB at "
                    f"{target} within {budget}s of launch. "
                    "Under TCG software emulation first boot is slow (minutes) — "
                    f"run `beetroot logs {self._name}` to watch the guest boot, give "
                    "it longer (raise BEETROOT_VM_ADB_CONNECT_TIMEOUT), or pin "
                    "`vm.accel: kvm` on a host with /dev/kvm for a faster boot."
                )
            time.sleep(_ADB_CONNECT_POLL_SECONDS)

    @staticmethod
    def _adb_connect_ok(target: str) -> bool:
        """
        Run a single ``adb connect <target>`` and report whether it attached.

        ``adb connect`` exits 0 even on a refused connection in some adb
        versions, so the stdout/stderr is re-scanned for ``failed`` /
        ``cannot connect`` as a safety net (mirrors
        :func:`beetroot.api._check_adb_connect`).

        Args:
            target: The ``host:port`` argument for ``adb connect``.

        Returns:
            True iff the attach succeeded.
        """
        try:
            res = subprocess.run(  # noqa: S603  # adb is a host CLI resolved via PATH; target is localhost:<port>
                [_ADB, "connect", target],
                check=False,
                capture_output=True,
                text=True,
                timeout=_ADB_CONNECT_ATTEMPT_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        combined = f"{res.stdout}\n{res.stderr}".lower()
        return res.returncode == 0 and "cannot connect" not in combined and "failed" not in combined

    def down(self) -> None:
        """
        Terminate the micro-VM (SIGTERM); a no-op if it isn't running.
        """
        self._qemu().terminate()

    def restart(self) -> None:
        """
        Terminate then re-launch the micro-VM.
        """
        self.down()
        self.up()

    def apply(self) -> None:
        """
        Re-load ``beetroot.yaml`` and re-stage the derived ``.env`` + dirs.

        Mirrors :meth:`beetroot.api.Instance.apply` for the redroid backend:
        re-reads the on-disk config (external edits picked up), re-renders
        the ``.env`` and re-stages modules so the guest's redroid boots with
        the new config on the next :meth:`restart`. Frida is not staged (it
        is unsupported on the vm backend — see :meth:`_stage`). If the config
        flips ``binder`` away from ``vm`` the registry row is reconciled back
        to the redroid kind.

        Raises:
            ValueError: If the re-resolved ports collide with another
                registered instance.
        """
        self._cfg = config.load_yaml(paths.instance_yaml(self._root))
        new_ports = ports.resolve_ports(self._index, self._cfg.ports)
        _check_port_collisions(self._name, new_ports)
        self._stage()
        self._warn_on_rootfs_version_skew()
        self._warn_on_inert_vm_config()
        registry.reconcile_backend_kind(self._name, self._cfg.binder)

    def _stage(self) -> None:
        """
        Render the ``.env`` + create dirs + stage modules (no Frida).

        Frida staging is deliberately omitted: the network-isolated guest
        can't read a bind-mounted frida-server, so staging one would be a
        no-op that misleads (issue #44). Only the ``.env`` render and
        module staging carry over from the redroid backend's ``_stage``.
        """
        from beetroot import modules_download  # noqa: PLC0415  # avoid import cycle

        paths.instance_data(self._root).mkdir(parents=True, exist_ok=True)
        paths.instance_modules(self._root).mkdir(parents=True, exist_ok=True)
        paths.instance_env(self._root).write_text(config.render_env(self._name, self._cfg))
        # The variable-length ports list lives in a per-instance compose
        # override (issue #108). The vm backend only forwards adb to qemu (the
        # override is consumed by the redroid compose path, not qemu), but it is
        # still staged for parity so a config flipped back to binder: host/auto
        # boots with the right ports without an extra apply.
        paths.instance_compose_override(self._root).write_text(
            config.render_compose_ports_override(self.ports)
        )
        modules_download.stage_for_instance(self._root, self._cfg)

    def destroy(self, *, yes: bool = False) -> None:
        """
        Terminate the micro-VM and permanently delete the instance directory.

        Args:
            yes: Must be ``True`` to proceed (the CLI confirms before calling
                with ``yes=True``). Passing ``False`` raises ``ValueError``.

        Raises:
            ValueError: If called with ``yes=False``.
        """
        if not yes:
            raise ValueError(
                "VmDeviceBackend.destroy() requires yes=True to proceed; "
                "confirm the destructive operation in the calling code first."
            )
        self._qemu().terminate()
        registry.remove(self._name)
        if self._root.exists():
            shutil.rmtree(self._root)

    # ---- health-check ----------------------------------------------------

    def health(self) -> dict[str, CheckResult]:
        """
        Aggregate the VM-backed health checks for this instance.

        Emits VM-specific rows rather than reusing the shared adb health set,
        which assumes a Magisk-flashed, USB-listed device the ``binder: vm``
        guest is not:

        * ``vm.process`` — is the QEMU micro-VM process alive?
        * ``vm.accel`` — resolved accelerator (kvm vs the slow-tcg note).
        * ``vm.qemu`` — is the QEMU emulator binary on ``PATH``? Without it the
          VM can never boot (issue #191), so a green ``vm.accel`` alone is not
          proof of readiness.
        * ``vm.artifacts`` — do the configured guest kernel + rootfs exist on the
          host? A missing artifact is a ``beetroot build --vm-kernel`` away
          (issue #191).
        * ``adb.connect`` — connect-then-verify against the forwarded loopback
          adb port. A TCP adb target only appears *after* an explicit ``adb
          connect``, so the USB-style always-listed ``adb.serial`` row false-
          fails a healthy VM from a fresh adb-server lifetime (issue #164).

        ``frida.handshake`` and the ``magisk.*`` rows are intentionally absent:
        the network-isolated guest has no Frida (issue #44) and boots an
        unmodified upstream redroid image with no Magisk (issue #163), so a
        permanent ``fail`` row would be noise.

        Returns:
            Ordered dict of check name → :class:`CheckResult`.
        """
        from beetroot.api import CheckResult  # noqa: PLC0415  # avoid import cycle with api.py

        checks: dict[str, CheckResult] = {}
        running = self._qemu().is_running()
        checks["vm.process"] = (
            CheckResult(status="pass")
            if running
            else CheckResult(status="fail", reason="QEMU micro-VM is not running")
        )
        checks["vm.accel"] = _accel_check(self._cfg.vm.accel)
        checks["vm.qemu"] = self._qemu_binary_check()
        checks["vm.artifacts"] = self._artifacts_check()
        checks["adb.connect"] = _check_adb_connect(self.adb_address)
        return checks

    def _qemu_binary_check(self) -> CheckResult:
        """
        Report whether the QEMU emulator binary is on ``PATH`` (issue #191).

        ``CheckResult`` has no dedicated remedy field, so the fix pointer is
        folded into the ``reason`` — reusing the shared
        :data:`capabilities._QEMU_INSTALL` string so the doctor row matches what
        ``beetroot modes`` prints.

        Returns:
            A ``pass`` row when ``settings.qemu_bin`` resolves, else a ``fail``
            row (a VM whose emulator is missing can never boot).
        """
        from beetroot.api import CheckResult  # noqa: PLC0415  # avoid import cycle with api.py

        if shutil.which(settings.qemu_bin) is not None:
            return CheckResult(status="pass")
        return CheckResult(
            status="fail",
            # Reuse the shared modes remedy verbatim (do not duplicate the literal).
            reason=(
                f"QEMU ({settings.qemu_bin}) not found on PATH — {capabilities._QEMU_INSTALL}"  # noqa: SLF001  # shared cross-module remedy string
            ),
        )

    def _artifacts_check(self) -> CheckResult:
        """
        Report whether the guest kernel + rootfs artifacts exist (issue #191).

        Resolves both artifacts through :func:`_resolve_artifact` (which raises
        :class:`qemu.QemuLaunchError` when the file is unset or missing). A green
        ``vm.accel`` alone would otherwise imply the VM can boot when the kernel
        or rootfs has never been built. The ``QemuLaunchError`` message already
        names the missing artifact and points at ``beetroot build --vm-kernel``.

        Returns:
            A ``pass`` row when both artifacts resolve, else a ``fail`` row whose
            reason names the missing artifact (the resolver's own message already
            points at ``beetroot build --vm-kernel``, matching
            :data:`capabilities._BUILD_HINT`).
        """
        from beetroot.api import CheckResult  # noqa: PLC0415  # avoid import cycle with api.py

        try:
            _resolve_artifact(self._cfg.vm.kernel, settings.vm_kernel, "kernel")
            _resolve_artifact(self._cfg.vm.rootfs, settings.vm_rootfs, "rootfs")
        except qemu.QemuLaunchError as exc:
            return CheckResult(status="fail", reason=str(exc))
        return CheckResult(status="pass")


def _accel_check(requested: Literal["auto", "kvm", "tcg"]) -> CheckResult:
    """
    Report the resolved accelerator as a doctor row, loudly flagging TCG.

    Args:
        requested: The instance's configured ``vm.accel`` value.

    Returns:
        A ``pass`` row for KVM (fast), a ``pass`` row for TCG with a loud
        perf note in the reason (~5-20x slowdown), or a ``fail`` row when an
        explicit ``kvm`` request can't be honoured on this host.
    """
    from beetroot.api import CheckResult  # noqa: PLC0415  # avoid import cycle with api.py

    try:
        accel = qemu.detect_accel(requested)
    except qemu.QemuLaunchError as exc:
        return CheckResult(status="fail", reason=str(exc))
    if accel == "kvm":
        return CheckResult(status="pass", reason="KVM-accelerated (near-native)")
    return CheckResult(
        status="pass",
        reason="TCG software emulation — no /dev/kvm; expect ~5-20x slower boots",
    )


register_backend("vm", VmDeviceBackend)

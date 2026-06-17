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
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Self

from beetroot import config, paths, ports, registry
from beetroot.api import (
    AdbNotInstalledError,
    BackendCapabilityError,
    InstanceNotFoundError,
    adb_device_health,
)
from beetroot.backends import register_backend
from beetroot.settings import settings
from beetroot.vm import qemu

if TYPE_CHECKING:
    from beetroot.api import CheckResult

_ADB = "adb"

# How often ``up`` re-tries ``adb connect`` against the freshly-launched
# guest, and the per-attempt subprocess timeout. The deadline itself is the
# configurable ``settings.vm_adb_connect_timeout`` — the guest restarts adbd
# to enable TCP a few seconds *after* ``sys.boot_completed=1``, so the first
# connect almost always races that late bind and must be retried.
_ADB_CONNECT_POLL_SECONDS = 1.0
_ADB_CONNECT_ATTEMPT_TIMEOUT = 5

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
    path = Path(raw)
    if not path.exists():
        raise qemu.QemuLaunchError(
            f"VM {label} {raw!r} does not exist on the host filesystem. "
            f"Build it with `beetroot build --vm-kernel`."
        )
    return path


def _check_port_collisions(name: str, new_ports: dict[str, int]) -> None:
    """
    Raise ``ValueError`` if ``new_ports`` collide with any other instance.

    Mirrors ``beetroot.api._check_port_collisions`` (kept local to avoid
    reaching into the api module's private surface) so a ``beetroot apply``
    on a VM instance catches a port clash the same way the redroid backend
    does.

    Args:
        name: This instance's registry name (excluded from the comparison).
        new_ports: The resolved port dict to validate.

    Raises:
        ValueError: On the first colliding port.
    """
    others = {n: p for n, p in registry.all_resolved_ports().items() if n != name}
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
    def ports(self) -> dict[str, int]:
        """
        Resolved host ports for this instance (``adb`` / ``frida`` / ``frida_control``).
        """
        return ports.resolve_ports(self._index, self._cfg.ports)

    @property
    def adb_address(self) -> str:
        """
        ``localhost:<adb_port>`` — the QEMU-forwarded guest adbd port.
        """
        return f"localhost:{self.ports['adb']}"

    @property
    def frida_address(self) -> str:
        """
        Report Frida as unsupported on the vm backend.

        Frida-over-VM is not yet wired through the network-isolated guest
        (issue #44), so this never names a reachable endpoint — it returns
        the sentinel ``"unsupported"`` so ``ls`` / ``status`` rows don't
        advertise a working ``localhost:<port>`` that Frida could never
        connect to. The frida verbs themselves raise
        :class:`~beetroot.api.BackendCapabilityError`.
        """
        return "unsupported"

    @property
    def is_available(self) -> bool:
        """
        True iff the QEMU process for this instance is alive.
        """
        return qemu.QemuProcess(self._root).is_running()

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
            host_adb_port=self.ports["adb"],
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
                expose ADB within ``settings.vm_adb_connect_timeout`` seconds.
        """
        if self._cfg.binder != "vm":
            raise BackendCapabilityError(
                f"instance {self._name!r} is registered as a vm backend but its "
                f"beetroot.yaml now sets binder: {self._cfg.binder!r}. The two are "
                f"out of sync — run `beetroot apply {self._name}` to reconcile the "
                "registry before `beetroot up`."
            )
        accel = self.resolved_accel()
        argv = self.build_argv(accel)
        qemu.QemuProcess(self._root).start(argv)
        self._wait_for_adb_connect()

    def _wait_for_adb_connect(self) -> None:
        """
        Poll ``adb connect`` against the guest until it accepts or times out.

        The guest forwards adbd to a host loopback port, but redroid restarts
        adbd to switch it into TCP mode a few seconds *after* boot completes,
        so the endpoint refuses connections briefly. Retry ``adb connect``
        every :data:`_ADB_CONNECT_POLL_SECONDS` until the host adb reports a
        successful attach or ``settings.vm_adb_connect_timeout`` elapses. The
        happy path (endpoint already up) succeeds on the first attempt and
        never sleeps.

        Raises:
            AdbNotInstalledError: If the ``adb`` binary is not on PATH.
            qemu.QemuLaunchError: If no attempt succeeds within the deadline —
                a friendly, actionable message (not a traceback).
        """
        if shutil.which(_ADB) is None:
            raise AdbNotInstalledError("adb not found on PATH (install android-tools)")
        target = self.adb_address
        deadline = time.monotonic() + settings.vm_adb_connect_timeout
        while True:
            if self._adb_connect_ok(target):
                return
            if time.monotonic() >= deadline:
                raise qemu.QemuLaunchError(
                    f"the QEMU micro-VM for {self._name!r} did not expose ADB at "
                    f"{target} within {settings.vm_adb_connect_timeout}s of launch. "
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
        qemu.QemuProcess(self._root).terminate()

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
        paths.instance_env(self._root).write_text(
            config.render_env(self._name, self._cfg, self.ports)
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
        qemu.QemuProcess(self._root).terminate()
        registry.remove(self._name)
        if self._root.exists():
            shutil.rmtree(self._root)

    # ---- health-check ----------------------------------------------------

    def health(self) -> dict[str, CheckResult]:
        """
        Aggregate the VM-backed health checks for this instance.

        Includes a VM-specific ``vm.process`` row (is QEMU alive?) and a
        ``vm.accel`` row (kvm vs the slow-tcg note), then the shared
        adb/magisk rows so downstream tools grep uniformly across backend
        kinds. The ``frida.handshake`` row is dropped: Frida is unsupported
        on the network-isolated guest (issue #44), so the handshake could
        never pass and a permanent ``fail`` row would be noise.

        Returns:
            Ordered dict of check name → :class:`CheckResult`.
        """
        from beetroot.api import CheckResult  # noqa: PLC0415  # avoid import cycle with api.py

        checks: dict[str, CheckResult] = {}
        running = qemu.QemuProcess(self._root).is_running()
        checks["vm.process"] = (
            CheckResult(status="pass")
            if running
            else CheckResult(status="fail", reason="QEMU micro-VM is not running")
        )
        checks["vm.accel"] = _accel_check(self._cfg.vm.accel)
        shared = adb_device_health(self)
        shared.pop("frida.handshake", None)
        checks.update(shared)
        return checks


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

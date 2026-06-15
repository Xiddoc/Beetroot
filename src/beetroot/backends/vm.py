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
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Self

from beetroot import config, frida_download, paths, ports, registry
from beetroot.api import (
    AdbNotInstalledError,
    FridaNotInstalledError,
    InstanceNotFoundError,
    adb_device_health,
)
from beetroot.backends import register_backend
from beetroot.settings import settings
from beetroot.vm import qemu

if TYPE_CHECKING:
    from beetroot.api import CheckResult

_ADB = "adb"
_FRIDA = "frida"


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
        """Registry name for this backend."""
        return self._name

    @property
    def kind(self) -> Literal["vm"]:
        """Backend discriminator — always ``"vm"``."""
        return "vm"

    @property
    def root(self) -> Path:
        """Absolute path to the instance directory."""
        return self._root

    @property
    def config(self) -> config.InstanceConfig:
        """The parsed ``beetroot.yaml`` for this instance."""
        return self._cfg

    @property
    def ports(self) -> dict[str, int]:
        """Resolved host ports for this instance (``adb`` / ``frida`` / ``frida_control``)."""
        return ports.resolve_ports(self._index, self._cfg.ports)

    @property
    def adb_address(self) -> str:
        """``localhost:<adb_port>`` — the QEMU-forwarded guest adbd port."""
        return f"localhost:{self.ports['adb']}"

    @property
    def frida_address(self) -> str:
        """``localhost:<frida_port>`` — what ``frida -H`` should target."""
        return f"localhost:{self.ports['frida']}"

    @property
    def is_available(self) -> bool:
        """True iff the QEMU process for this instance is alive."""
        return qemu.QemuProcess(self._root).is_running()

    def install_frida(self, version: str | None = None) -> None:
        """
        Stage a frida-server binary for this instance's VM guest.

        Downloads the requested frida-server into the per-user cache
        (idempotent) and copies it into the instance's bind-mount slot, so
        the guest's boot wiring can launch it. A subsequent :meth:`restart`
        is required for the guest to pick up a new binary.

        Args:
            version: The frida release tag (e.g. ``16.4.10``). ``None`` uses
                the version pinned in this instance's ``beetroot.yaml``
                (``cfg.frida.version``).

        Raises:
            ValueError: If ``version`` is ``None`` and the instance has no
                ``frida:`` block in its config.
        """
        if version is None:
            if self._cfg.frida is None:
                raise ValueError(
                    f"instance {self._name!r} has no frida: block in its config; "
                    "pass a version explicitly (e.g. install_frida('16.4.10'))"
                )
            version = self._cfg.frida.version
        frida_download.stage_for_instance(self._root, version)

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
        Invoke the host ``frida`` CLI against this instance's guest.

        Beetroot prepends ``-H localhost:<frida_port>`` and forwards the
        rest of ``args`` verbatim.

        Args:
            args: Tokens to pass after ``frida -H <addr>``.

        Returns:
            The exit code of the ``frida`` invocation.

        Raises:
            FridaNotInstalledError: If the ``frida`` binary is not on PATH.
        """
        if shutil.which(_FRIDA) is None:
            raise FridaNotInstalledError(
                "frida CLI not found. "
                "Install via `uv tool install 'beetroot[frida]'` "
                "or `uv tool install frida-tools`."
            )
        cmd = [_FRIDA, "-H", self.frida_address, *args]
        res = subprocess.run(cmd, check=False)  # noqa: S603  # frida is a host CLI resolved via PATH; argv validated upstream
        return int(res.returncode)

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
            smp=self._cfg.vm.smp,
            memory_mib=self._cfg.vm.memory_mib,
            host_adb_port=self.ports["adb"],
        )

    def up(self) -> None:
        """
        Boot the micro-VM: resolve accel, build argv, launch QEMU detached.

        Raises:
            qemu.QemuLaunchError: On a missing accelerator, missing
                kernel/rootfs, or a launch failure.
        """
        accel = self.resolved_accel()
        argv = self.build_argv(accel)
        qemu.QemuProcess(self._root).start(argv)

    def down(self) -> None:
        """Terminate the micro-VM (SIGTERM); a no-op if it isn't running."""
        qemu.QemuProcess(self._root).terminate()

    def restart(self) -> None:
        """Terminate then re-launch the micro-VM."""
        self.down()
        self.up()

    # ---- health-check ----------------------------------------------------

    def health(self) -> dict[str, CheckResult]:
        """
        Aggregate the VM-backed health checks for this instance.

        Includes a VM-specific ``vm.process`` row (is QEMU alive?) and a
        ``vm.accel`` row (kvm vs the slow-tcg note), then the shared
        adb/frida/magisk rows so downstream tools grep uniformly across
        backend kinds.

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
        checks.update(adb_device_health(self))
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

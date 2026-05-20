"""
``AdbDevice`` backend — real (or emulator) Android device driven over ADB.

The :class:`beetroot.api.Instance` class is the v0.3 Redroid-over-compose
backend; ``AdbDevice`` is its sibling for any rooted Android device that
the host can reach via ``adb`` (a real phone on USB, an emulator started
outside Beetroot, a ``adb connect``-ed network device — anything where
``adb devices`` lists the target with state ``"device"``).

The class satisfies :class:`beetroot.api.DeviceBackend` so every
Protocol-driven CLI verb (``shell``, ``frida``, ``module``, ``env``,
``status``, ``doctor``) Just Works against an adb-adopted instance.
Lifecycle verbs that only make sense for a managed container
(``up``, ``down``, ``restart``, ``apply``, ``destroy``, ``snapshot``)
raise :class:`beetroot.api.BackendCapabilityError` — the CLI catches
it and renders a friendly ``error: ...`` line + ``exit 2``.

Per-host port allocation re-uses the stride-of-10 scheme from
:mod:`beetroot.ports` so an adb-adopted instance never collides with
the Frida control ports a redroid container would pick. The ``host``
side of ``adb forward tcp:<host_port> tcp:27042`` is the resolved
``frida`` port for the instance's registry index — exactly the port
the user would have got if they'd called ``beetroot create`` instead
of ``beetroot adopt``.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from beetroot import frida_download, ports, registry
from beetroot.api import (
    AdbNotInstalledError,
    BackendCapabilityError,
    FridaNotInstalledError,
    adb_device_health,
)
from beetroot.backends import register_backend

if TYPE_CHECKING:
    from beetroot.api import CheckResult

_ADB = "adb"
_FRIDA = "frida"
_REMOTE_FRIDA_SERVER = "/data/local/tmp/frida-server"
_DEVICE_PORT = 27042
_MAGISK_MODULE_DROP = "/sdcard/Download"

# Number of whitespace-separated columns the ``adb devices`` output
# produces for an entry — the first is the serial, the second is the
# state (``device`` / ``offline`` / ``unauthorized`` / ...). Extracted
# as a constant to keep the ``is_available`` parser readable under
# ruff's PLR2004 (no-magic-numbers) gate.
_ADB_DEVICES_COLUMNS = 2


def serial_is_available(serial: str) -> bool:
    """
    Return True iff ``adb devices`` lists ``serial`` in state ``"device"``.

    Shared between :attr:`AdbDevice.is_available` and the ``--verify``
    flag on ``beetroot adopt`` so both call sites use identical parsing
    logic. Serials in ``offline`` / ``unauthorized`` / ``no permissions``
    state return False — the user needs to re-plug, accept the RSA prompt,
    or fix udev rules first.

    Args:
        serial: The adb serial / endpoint identifier to look up.

    Returns:
        True if ``adb devices`` exits 0 and the serial is listed as
        ``device``; False otherwise.
    """
    res = subprocess.run(  # noqa: S603  # adb is a host CLI on PATH; argv is constant
        [_ADB, "devices"],
        check=False,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return False
    for line in res.stdout.splitlines():
        parts = line.split()
        if (
            len(parts) >= _ADB_DEVICES_COLUMNS
            and parts[0] == serial
            and parts[1] == "device"
        ):
            return True
    return False


class AdbDevice:
    """
    Backend that drives a rooted Android device via the host ``adb`` CLI.

    Attributes:
        _name: Registry name for this backend.
        _config: The validated :class:`registry.AdbBackendConfig` row.
        _host_forward_port: Host port number that ``adb forward
            tcp:<host_forward_port> tcp:27042`` exposes for Frida.
    """

    def __init__(
        self,
        name: str,
        config: registry.AdbBackendConfig,
        host_forward_port: int,
    ) -> None:
        """
        Bind a name + adb config + reserved host port into an ``AdbDevice``.

        Most callers use :meth:`from_meta` (which derives the host port
        from the registry meta's allocated index) instead of this
        low-level constructor.

        Args:
            name: Registry name for this backend.
            config: The validated :class:`registry.AdbBackendConfig` row.
            host_forward_port: Host port number for the Frida ``adb
                forward`` mapping.
        """
        self._name = name
        self._config = config
        self._host_forward_port = host_forward_port

    @classmethod
    def from_meta(
        cls, name: str, backend: registry.BackendConfig,
    ) -> AdbDevice:
        """
        Build an :class:`AdbDevice` from a registry meta's backend config.

        Used by :meth:`beetroot.api.Manager.resolve` to dispatch via the
        backend registry. The host forward port is derived from the
        registry meta's allocated index via the same stride-of-10
        allocator used for redroid instances — so an adb-backed
        instance with index ``N`` shares the Frida port a redroid
        instance with index ``N`` would have got.

        Args:
            name: Registry name.
            backend: The matching :class:`registry.AdbBackendConfig`
                row's backend field.

        Returns:
            The hydrated :class:`AdbDevice`.

        Raises:
            TypeError: If ``backend`` is not an
                :class:`registry.AdbBackendConfig` (a registry shape
                error caught by ``Manager.resolve`` upstream).
        """
        if not isinstance(backend, registry.AdbBackendConfig):
            raise TypeError(
                f"AdbDevice expected AdbBackendConfig, got "
                f"{type(backend).__name__}",
            )
        meta = registry.get(name)
        if meta is None:
            raise LookupError(
                f"no instance named {name!r} in registry; "
                "cannot derive host forward port",
            )
        host_port = ports.ports_for_index(meta.index)["frida"]
        return cls(name=name, config=backend, host_forward_port=host_port)

    # ---- DeviceBackend Protocol surface -----------------------------------

    @property
    def name(self) -> str:
        """Registry name for this backend."""
        return self._name

    @property
    def kind(self) -> Literal["adb"]:
        """Backend discriminator — always ``"adb"``."""
        return "adb"

    @property
    def adb_address(self) -> str:
        """
        Return the adb serial verbatim (the value of ``adb -s <serial>``).

        Adb-backed devices don't have a host:port form (the serial IS
        the address); the property name matches the Protocol so callers
        can stay backend-agnostic.
        """
        return self._config.serial

    @property
    def frida_address(self) -> str:
        """``localhost:<host_forward_port>`` — what ``frida -H`` should target."""
        return f"localhost:{self._host_forward_port}"

    @property
    def is_available(self) -> bool:
        """
        True iff ``adb devices`` lists this serial in state ``"device"``.

        Devices that show up as ``offline`` / ``unauthorized`` /
        ``no permissions`` count as unavailable — the user needs to
        re-plug, accept the RSA prompt, or fix udev rules first.
        """
        return serial_is_available(self._config.serial)

    def install_frida(self, version: str) -> None:
        """
        Download the requested frida-server, push it, launch it, expose it.

        Steps:
        1. ``frida_download.download(version)`` populates the per-user
           cache (idempotent — re-runs hit the cached binary).
        2. ``adb push`` the cached binary to ``/data/local/tmp/frida-server``.
        3. ``adb shell chmod 755`` so the binary is executable.
        4. ``adb shell su -c '/data/local/tmp/frida-server &'`` to
           background the daemon. Requires the device to be rooted
           (Magisk / KernelSU / SuperSU all work).
        5. ``adb forward tcp:<host_port> tcp:27042`` so ``frida -H
           localhost:<host_port>`` reaches the device's Frida socket.

        Args:
            version: The frida release tag (e.g. ``16.4.10``).

        Raises:
            AdbNotInstalledError: If the ``adb`` binary is not on PATH.
        """
        import shutil  # noqa: PLC0415  # local to avoid pulling shutil at module import

        if shutil.which(_ADB) is None:
            raise AdbNotInstalledError(
                "adb not found on PATH (install android-tools)",
            )
        cached = frida_download.download(version)
        self._adb("push", str(cached), _REMOTE_FRIDA_SERVER)
        self._adb_shell(["chmod", "755", _REMOTE_FRIDA_SERVER])
        # Background the server via ``su -c`` — quote handling is the
        # caller's responsibility because adb shell strips the outer
        # quotes; we pass the command as a single argv element so the
        # ``&`` reaches the on-device shell, not the host shell.
        self._adb_shell(["su", "-c", f"{_REMOTE_FRIDA_SERVER} &"])
        self._adb(
            "forward",
            f"tcp:{self._host_forward_port}",
            f"tcp:{_DEVICE_PORT}",
        )

    def shell(self) -> int:
        """
        Open an interactive ``adb -s <serial> shell``.

        Unlike :class:`beetroot.api.Instance.shell`, no preliminary
        ``adb connect`` is needed — the user-supplied serial already
        identifies a connected device.

        Returns:
            The exit code of the ``adb shell`` invocation.

        Raises:
            AdbNotInstalledError: If the ``adb`` binary is not on PATH.
        """
        import shutil  # noqa: PLC0415  # local to avoid pulling shutil at module import

        if shutil.which(_ADB) is None:
            raise AdbNotInstalledError(
                "adb not found on PATH (install android-tools)",
            )
        res = subprocess.run(  # noqa: S603  # adb is a host CLI on PATH; argv is constant + user-pinned serial
            [_ADB, "-s", self._config.serial, "shell"],
            check=False,
        )
        return int(res.returncode)

    def frida_cli(self, args: list[str]) -> int:
        """
        Invoke the host ``frida`` CLI against this device.

        Beetroot prepends ``-H localhost:<host_forward_port>`` and
        forwards the rest of ``args`` verbatim — mirrors
        :meth:`beetroot.api.Instance.frida_cli` so the ``beetroot
        frida`` verb is uniform across backends.

        Args:
            args: Tokens to pass after ``frida -H <addr>`` (e.g.
                ``["-n", "com.app"]``).

        Returns:
            The exit code of the ``frida`` invocation.

        Raises:
            FridaNotInstalledError: If the ``frida`` binary is not on
                PATH (install via the ``[frida]`` extra).
        """
        import shutil  # noqa: PLC0415  # local to avoid pulling shutil at module import

        if shutil.which(_FRIDA) is None:
            raise FridaNotInstalledError(
                "frida CLI not found. "
                "Install via `uv tool install 'beetroot[frida]'` "
                "or `uv tool install frida-tools`.",
            )
        cmd = [_FRIDA, "-H", self.frida_address, *args]
        res = subprocess.run(cmd, check=False)  # noqa: S603  # frida is a host CLI resolved via PATH; argv validated upstream
        return int(res.returncode)

    def add_module(self, source: str, *, sha256: str | None = None) -> None:
        """
        Push a Magisk module zip to the device's Downloads dir.

        v0.4 ships the safe-default variant: the zip is pushed to
        ``/sdcard/Download/<basename>`` and the user is told to install
        it via the Magisk app's Modules tab. The auto-install variant
        (push directly to ``/data/adb/modules_update/`` via ``su -c``)
        is deferred to v0.6 because it requires extra UX to surface
        per-module success/failure without booting the device into a
        bad state.

        Args:
            source: Path to a local ``.zip`` on the host filesystem.
                Remote URLs are deliberately not supported here — the
                user can ``curl`` the zip into ``./modules/`` first if
                they want the same UX as :class:`beetroot.api.Instance`.
            sha256: Optional expected hex digest for integrity checking.
                Currently advisory only — the v0.6 auto-install variant
                will enforce it; for v0.4 the host-side hash is the
                user's responsibility before invoking ``beetroot module``.
        """
        del sha256  # Reserved for the v0.6 auto-install variant.
        src = Path(source)
        if not src.exists():
            raise ValueError(
                f"module source {source!r} does not exist on the host filesystem; "
                "download the zip first and pass its local path.",
            )
        if not src.is_file():
            raise ValueError(
                f"module source {source!r} is a directory, not a zip file; "
                "pass the path to the .zip itself.",
            )
        if src.suffix.lower() != ".zip":
            raise ValueError(
                f"module source {source!r} does not end in .zip; "
                "Magisk modules must be packaged as zip archives.",
            )
        basename = src.name
        remote = f"{_MAGISK_MODULE_DROP}/{basename}"
        self._adb("push", str(src), remote)
        # User-facing instruction — print() rather than typer.echo()
        # so callers that exercise the Protocol surface directly (not
        # via the CLI) still see the message.
        print(  # noqa: T201  # user-facing instruction
            f"[beetroot] pushed {basename} → {remote}. "
            f"Install via the Magisk app → Modules tab → Install from "
            f"storage; pick {remote}.",
            file=sys.stderr,
        )

    # ---- BackendCapability stubs (lifecycle verbs) ------------------------

    def up(self) -> None:
        """Raise :class:`BackendCapabilityError` — adb devices are always-on."""
        raise BackendCapabilityError(
            f"up is not supported for adb-backed instance {self._name!r}; "
            "the device is managed outside Beetroot and is always on",
        )

    def down(self) -> None:
        """Raise :class:`BackendCapabilityError` — adb devices are always-on."""
        raise BackendCapabilityError(
            f"down is not supported for adb-backed instance {self._name!r}; "
            f"the device is managed outside Beetroot. "
            f"Use `beetroot forget {self._name}` to deregister.",
        )

    def restart(self) -> None:
        """Raise :class:`BackendCapabilityError` — adb devices are always-on."""
        raise BackendCapabilityError(
            f"restart is not supported for adb-backed instance "
            f"{self._name!r}; reboot the device via `adb reboot` instead.",
        )

    def apply(self) -> None:
        """Raise :class:`BackendCapabilityError` — adb instances have no yaml."""
        raise BackendCapabilityError(
            f"apply is not supported for adb-backed instance "
            f"{self._name!r}; there is no beetroot.yaml to re-render.",
        )

    def destroy(self, *, yes: bool = False) -> None:  # noqa: ARG002  # ``yes`` mirrors Instance.destroy for verb parity
        """Raise :class:`BackendCapabilityError` — adb instances live outside Beetroot."""
        raise BackendCapabilityError(
            f"destroy is not supported for adb-backed instance "
            f"{self._name!r}; the device is managed outside Beetroot. "
            f"Use `beetroot forget {self._name}` to deregister.",
        )

    def snapshot(self, dest: Path) -> Path:  # noqa: ARG002  # ``dest`` mirrors Instance.snapshot for verb parity
        """Raise :class:`BackendCapabilityError` — no host-side state to pack."""
        raise BackendCapabilityError(
            f"snapshot is not supported for adb-backed instance "
            f"{self._name!r}; there is no host-side directory to pack.",
        )

    # ---- health-check ----------------------------------------------------

    def health(self) -> dict[str, CheckResult]:
        """
        Aggregate the adb-backed health checks for this device.

        T7 wired this on as a real method (T6 shipped the body as a free
        function in :mod:`beetroot.api` because T6 landed before T5's
        :class:`AdbDevice` class existed). The free function
        :func:`beetroot.api.adb_device_health` is preserved as a thin
        shim that delegates here, so existing programmatic callers
        (and the doctor-verb dispatch path that predates this method)
        keep working unchanged.

        The check NAMES (``frida.handshake``, ``magisk.zygisk``,
        ``magisk.denylist.<pkg>``) match :meth:`beetroot.api.Instance.health`
        exactly so downstream tools can grep uniformly across backend
        kinds. ``compose.status`` is intentionally absent — there's no
        container for an adb-backed device.

        Returns:
            Ordered dict of check name → :class:`CheckResult`.
        """
        return adb_device_health(self)

    # ---- internals --------------------------------------------------------

    def _adb(self, *argv: str) -> subprocess.CompletedProcess[str]:
        """Run ``adb -s <serial> <argv...>`` with capture; raise on non-zero."""
        full = [_ADB, "-s", self._config.serial, *argv]
        res = subprocess.run(  # noqa: S603  # adb is a host CLI on PATH; argv built from validated config + caller-pinned strings
            full,
            check=False,
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            raise RuntimeError(
                f"adb command {full!r} failed (rc={res.returncode}): "
                f"{res.stderr.strip()}",
            )
        return res

    def _adb_shell(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        """Run ``adb -s <serial> shell <argv...>`` with capture; raise on non-zero."""
        return self._adb("shell", *argv)


register_backend("adb", AdbDevice)

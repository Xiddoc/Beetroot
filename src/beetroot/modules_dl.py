"""
Stage Magisk module zips into an instance's ``modules/`` directory.

Each module entry in ``beetroot.yaml`` is either a URL (downloaded and
cached) or a host path. Relative ``path:`` entries are resolved relative
to the instance directory itself (the one containing ``beetroot.yaml``).
An optional ``sha256`` field is verified when present to guard against
corruption or supply-chain substitution.
"""
from __future__ import annotations

import shutil
import urllib.error
import urllib.request
from pathlib import Path

from . import frida_dl, paths
from .config import InstanceConfig, Module
from .settings import settings

# Only HTTP(S) module URLs are allowed. Allowing ``file://`` would let a
# malicious ``beetroot.yaml`` exfiltrate arbitrary host files into the
# module cache (e.g. ``url: file:///etc/passwd``). Other schemes
# (``ftp:``, ``gopher:``) are not in scope either.
_ALLOWED_URL_SCHEMES: tuple[str, ...] = ("http://", "https://")


class ModuleFetchError(RuntimeError):
    """Raised when a module zip cannot be downloaded from its URL."""


def _module_cache_dir() -> Path:
    return paths.user_cache_dir("modules")


def _filename_from_url(url: str) -> str:
    """Return the basename of the URL path, or ``module.zip`` if empty."""
    return url.rsplit("/", 1)[-1] or "module.zip"


def _fetch_url(url: str) -> Path:
    if not url.startswith(_ALLOWED_URL_SCHEMES):
        # Belt-and-suspenders: the Module pydantic validator already
        # rejects non-http(s) schemes, but defending here too means a
        # raw _fetch_url call from a third-party script can't bypass
        # the allowlist via a hand-built string.
        raise ModuleFetchError(
            f"module url {url!r} uses an unsupported scheme; "
            "only http:// and https:// are allowed"
        )
    cache = _module_cache_dir() / _filename_from_url(url)
    if cache.exists() and cache.stat().st_size > 0:
        return cache
    cache.parent.mkdir(parents=True, exist_ok=True)
    print(f"[beetroot] fetching module {url}")  # noqa: T201  # researcher-facing stdout; replacing with logging would change UX
    try:
        with urllib.request.urlopen(url, timeout=settings.http_timeout) as resp:  # noqa: S310  # scheme validated by Module pydantic model + _fetch_url allowlist
            data = resp.read()
    except urllib.error.HTTPError as e:
        raise ModuleFetchError(
            f"download failed: HTTP {e.code} fetching {url}; "
            "verify the URL is current (the upstream release may have moved)"
        ) from e
    except TimeoutError as e:
        raise ModuleFetchError(
            f"download timed out after {settings.http_timeout}s: {url}"
        ) from e
    except urllib.error.URLError as e:
        raise ModuleFetchError(
            f"download failed: cannot reach {url}: {e.reason}"
        ) from e
    tmp = cache.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(cache)
    return cache


def _resolve(module: Module, instance_root: Path) -> Path:
    if module.url:
        local = _fetch_url(module.url)
    else:
        # The Module pydantic validator already enforces "exactly one
        # of url / path"; this branch is a defensive net for mypy
        # narrowing and isn't covered.
        if module.path is None:  # pragma: no cover
            raise ValueError("module entry has neither url nor path set")
        local = (instance_root / module.path).resolve()
        if not local.exists():
            raise FileNotFoundError(f"module path not found: {local}")
    if module.sha256:
        actual = frida_dl.sha256_of(local)
        if actual.lower() != module.sha256.lower():
            raise ValueError(
                f"sha256 mismatch for {local.name}: "
                f"expected {module.sha256}, got {actual}"
            )
    return local


def stage_for_instance(instance_root: Path, cfg: InstanceConfig) -> list[Path]:
    """
    Materialise all module zips into ``<instance_root>/modules/``. Idempotent.

    Wipes stale zips before staging so that removing a module from
    ``beetroot.yaml`` actually un-stages it on the next ``apply``.

    Args:
        instance_root: The instance directory (the one containing
            ``beetroot.yaml``). Relative ``path:`` entries in the config
            are resolved relative to this directory.
        cfg: The instance configuration containing the modules list.

    Returns:
        List of paths to the staged zip files inside the instance directory.
    """
    target = paths.instance_modules(instance_root)
    target.mkdir(parents=True, exist_ok=True)
    for stale in target.glob("*.zip"):
        stale.unlink()
    staged: list[Path] = []
    for module in cfg.modules:
        src = _resolve(module, instance_root)
        dst = target / src.name
        shutil.copyfile(src, dst)
        staged.append(dst)
    return staged

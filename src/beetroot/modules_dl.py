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


def _module_cache_dir() -> Path:
    return paths.user_cache_dir("modules")


def _filename_from_url(url: str) -> str:
    """Return the basename of the URL path, or ``module.zip`` if empty."""
    return url.rsplit("/", 1)[-1] or "module.zip"


def _fetch_url(url: str) -> Path:
    cache = _module_cache_dir() / _filename_from_url(url)
    if cache.exists() and cache.stat().st_size > 0:
        return cache
    cache.parent.mkdir(parents=True, exist_ok=True)
    print(f"[beetroot] fetching module {url}")
    try:
        with urllib.request.urlopen(url, timeout=settings.http_timeout) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"download failed: HTTP {e.code} fetching {url}") from e
    except TimeoutError as e:
        raise RuntimeError(
            f"download timed out after {settings.http_timeout}s: {url}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"download failed: cannot reach {url}: {e.reason}") from e
    tmp = cache.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(cache)
    return cache


def _resolve(module: Module, instance_root: Path) -> Path:
    if module.url:
        local = _fetch_url(module.url)
    else:
        assert module.path is not None  # validated in Module model
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

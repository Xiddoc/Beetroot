"""
Stage Magisk module zips into ``instances/<name>/modules/``.

Each module entry in ``beetroot.yaml`` is either a URL (downloaded and
cached) or a host-relative path (copied directly). An optional
``sha256`` field is verified when present to guard against corruption or
supply-chain substitution.
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
    return paths.repo_root() / ".cache" / "modules"


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


def _resolve(module: Module) -> Path:
    if module.url:
        local = _fetch_url(module.url)
    else:
        assert module.path is not None  # validated in Module model
        local = (paths.repo_root() / module.path).resolve()
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


def stage_for_instance(name: str, cfg: InstanceConfig) -> list[Path]:
    """
    Materialise all module zips into ``instances/<name>/modules/``. Idempotent.

    Wipes stale zips before staging so that removing a module from
    ``beetroot.yaml`` actually un-stages it on the next ``apply``.

    Args:
        name: Instance name.
        cfg: The instance configuration containing the modules list.

    Returns:
        List of paths to the staged zip files inside the instance directory.
    """
    target = paths.instance_modules(name)
    target.mkdir(parents=True, exist_ok=True)
    # Wipe any stale zips first so removing a module from beetroot.yaml
    # actually un-stages it on next apply.
    for stale in target.glob("*.zip"):
        stale.unlink()
    staged: list[Path] = []
    for module in cfg.modules:
        src = _resolve(module)
        dst = target / src.name
        shutil.copyfile(src, dst)
        staged.append(dst)
    return staged

"""Integrity guard for the LSPosed module-hook e2e fixture.

``tests/fixtures/xposed-hook-module/beetroot-hook-test.apk`` is a prebuilt,
signed Xposed module that the LSPosed module-hook e2e installs and scopes to a
target app; when its hooks fire it logs ``BEETROOT_HOOK_FIRED`` to LSPosed's
module log. These tests pin the fixture's shape (a stale or corrupted apk would
silently break the e2e) without needing the Android toolchain.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "xposed-hook-module"
APK = FIXTURE / "beetroot-hook-test.apk"

_MODULE_CLASS = "party.beetroot.hooktest.Init"


def test_apk_is_a_valid_zip_with_module_parts() -> None:
    assert APK.is_file(), f"missing fixture apk: {APK}"
    with zipfile.ZipFile(APK) as z:
        names = set(z.namelist())
    # An installable Xposed module needs its code, the manifest, and the
    # classic-Xposed entry-point declaration.
    assert "classes.dex" in names
    assert "AndroidManifest.xml" in names
    assert "assets/xposed_init" in names
    # Signed apks carry a v1 signature block too (apksigner).
    assert any(n.startswith("META-INF/") for n in names)


def test_xposed_init_names_the_module_entry_class() -> None:
    with zipfile.ZipFile(APK) as z:
        init = z.read("assets/xposed_init").decode().strip()
    assert init == _MODULE_CLASS, init


def test_dex_defines_the_module_and_carries_the_hook_tags() -> None:
    with zipfile.ZipFile(APK) as z:
        dex = z.read("classes.dex")
    # The module's own class is present…
    assert b"party/beetroot/hooktest/Init" in dex
    # …and the observable hook markers the e2e greps for.
    assert b"BEETROOT_HOOK_FIRED" in dex
    assert b"BEETROOT_HOOK_LOADED" in dex


def test_source_hooks_activity_lifecycle_methods() -> None:
    src = (FIXTURE / "src" / "party" / "beetroot" / "hooktest" / "Init.java").read_text()
    assert "implements IXposedHookLoadPackage" in src
    assert "findAndHookMethod" in src
    # A real method hook (not just module load) is what makes this an end-to-end
    # *hook* test: onCreate/onResume on android.app.Activity.
    assert "android.app.Activity" in src
    assert "onResume" in src


def test_findandhookmethod_stub_returns_unhook() -> None:
    # Regression guard for the gotcha that cost an iteration: Vector matches the
    # full (remapped) method descriptor, so the compile-only stub's return type
    # must be XC_MethodHook.Unhook — getting it wrong yields a runtime
    # NoSuchMethodError.
    helpers = (
        FIXTURE / "stubs" / "de" / "robv" / "android" / "xposed" / "XposedHelpers.java"
    ).read_text()
    assert "XC_MethodHook.Unhook findAndHookMethod" in helpers

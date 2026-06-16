# Credits

Beetroot is a thin orchestration layer on top of several excellent open-source projects. None of this would exist without them.

## redroid

[redroid](https://github.com/remote-android/redroid-doc) — the Android-in-a-container project that makes running Android on a standard Linux host possible. redroid maps Android's kernel requirements (`binder`) onto the host kernel, avoiding the full overhead of a VM while keeping the Android userland intact.

## ayasa520/redroid-script

[`ayasa520/redroid-script`](https://github.com/ayasa520/redroid-script) — the patcher that Beetroot's `beetroot build` verb uses to bake Magisk, LiteGapps, and Houdini into a redroid base image. Without this patcher, getting root + GApps + ARM translation into the image would require significant manual work.

## Magisk

[Magisk](https://github.com/topjohnwu/Magisk) — the systemless root and module framework. Beetroot uses Magisk for:

- **Root access** via Zygisk.
- **Denylist** to hide root from GMS and target apps.
- **Module system** for flashing Shamiko and other overlays.
- **`magisk --sqlite`** for scriptable DB configuration from `entrypoint.sh`.

## Frida

[Frida](https://frida.re/) — the dynamic instrumentation toolkit. Beetroot manages the Frida server binary per-instance, downloading it from GitHub releases and bind-mounting it into the container. `beetroot frida` wraps the host-side CLI for convenience.

## Shamiko / LSPosed

[LSPosed/Shamiko](https://github.com/LSPosed/LSPosed.github.io) — a Magisk module that upgrades the denylist from "deny root access" to "hide Magisk's existence entirely" for denylisted processes. The [`examples/stealth.yaml`](../guides/examples.md) starter config ships a commented-out Shamiko `modules:` block ready to uncomment — the release URL is left to the user to fill in because LSPosed re-tags Shamiko releases, so a hard-coded URL would go stale silently.

## LiteGapps

[LiteGapps](https://litegapps.github.io/) — a minimal Google Mobile Services package that provides just enough of GMS for apps that require it, without the full bloat of a standard GApps package.

## Houdini

Intel's ARM-on-x86\_64 binary translation layer, included in the base image via the redroid-script patcher. Enables running ARM-compiled APKs on x86\_64 research hosts without recompiling.

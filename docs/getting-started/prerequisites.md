# Prerequisites

Beetroot runs Android inside a Docker container using [redroid](https://github.com/remote-android/redroid-doc). redroid works by mapping the Android `binder` kernel driver onto the host kernel, so your host needs to satisfy a few requirements before anything else.

!!! warning "Linux only"
    redroid relies on host kernel features that don't exist on macOS or Windows. A Linux host — physical or VM — is required. WSL2 is **not** supported.

## Host OS

Any modern Linux distribution works. Tested regularly on:

- Ubuntu 22.04 / 24.04 (easiest — kernel extras available via `apt`)
- Debian 12
- Arch Linux (with the `linux-headers` package)

## Kernel modules

redroid needs the `binder_linux` kernel module (Android 12+ uses memfd in place of ashmem, so no separate ashmem module is required for the default Android-14 base image).

=== "Ubuntu / Debian"

    ```bash
    sudo apt install linux-modules-extra-$(uname -r)
    sudo modprobe binder_linux
    ```

    To load it automatically at boot:

    ```bash
    echo 'binder_linux' | sudo tee /etc/modules-load.d/redroid.conf
    ```

=== "Arch Linux"

    ```bash
    # Install the binder module
    yay -S binder_linux-dkms
    sudo modprobe binder_linux
    ```

Verify with `lsmod | grep binder` — it should appear.

## Docker

Install Docker Engine (not Docker Desktop) and the Compose plugin:

=== "Ubuntu / Debian"

    ```bash
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker $USER
    # Log out and back in for the group to take effect
    ```

=== "Arch Linux"

    ```bash
    sudo pacman -S docker docker-compose
    sudo systemctl enable --now docker
    sudo usermod -aG docker $USER
    ```

Verify: `docker compose version` should print a version string.

!!! note "Privileged containers"
    Beetroot's containers run with `privileged: true`. This is required for Android init and Magisk to function correctly. Note that `privileged` does **not** substitute for the binder kernel driver — binder is a host-kernel feature, so a host without it can't run redroid even with full privileges. `beetroot doctor <name>` reports a `host.binder` row that tells you whether the kernel is ready.

!!! tip "No kernel access? (CI, cloud sandboxes)"
    If you can't load `binder_linux` on your host (a locked-down PaaS container, a kernel built without `CONFIG_ANDROID_BINDER_IPC`, macOS/Windows), redroid can't run there at all. You can still use the rest of Beetroot against a rooted device that lives **elsewhere** via `beetroot adopt` — see [Running in CI / without kernel access](../guides/running-in-ci.md).

## uv (Python runtime)

The `beetroot` CLI is a Python package managed with [uv](https://github.com/astral-sh/uv):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

`uv` handles the virtual environment and package installation automatically — you never need to call `pip` or activate a venv yourself.

## ADB

ADB is used by `beetroot shell` to attach to a running instance:

=== "Ubuntu / Debian"

    ```bash
    sudo apt install android-tools-adb
    ```

=== "Arch Linux"

    ```bash
    sudo pacman -S android-tools
    ```

=== "Manual"

    Download the [Android SDK Platform-Tools](https://developer.android.com/tools/releases/platform-tools) archive and put `adb` on your `PATH`.

## Optional: Frida CLI

If you want `beetroot frida <name>` to work, install the Frida CLI alongside Beetroot via the `[frida]` extra:

```bash
uv tool install 'beetroot[frida]'
```

Plain installs (`uv tool install ...` without `[frida]`) omit `frida-tools`; you can always add it later or install separately with `uv tool install frida-tools`.

The Frida *server* binary is managed entirely by Beetroot — you don't need to install it separately.

## Summary checklist

- [ ] Linux host (physical or VM)
- [ ] `binder_linux` kernel module loaded
- [ ] Docker Engine + Compose plugin installed; current user in `docker` group
- [ ] `uv` installed and on `PATH`
- [ ] `adb` on `PATH`
- [ ] (Optional) `frida` CLI available if you want `beetroot frida`

Once all boxes are checked, move on to [Installation](installation.md).

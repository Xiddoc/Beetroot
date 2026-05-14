# Prerequisites

Beetroot runs Android inside a Docker container using [redroid](https://github.com/remote-android/redroid-doc). redroid works by mapping Android kernel drivers (`binder`, `ashmem`) onto the host kernel, so your host needs to satisfy a few requirements before anything else.

!!! warning "Linux only"
    redroid relies on host kernel features that don't exist on macOS or Windows. A Linux host — physical or VM — is required. WSL2 is **not** supported.

## Host OS

Any modern Linux distribution works. Tested regularly on:

- Ubuntu 22.04 / 24.04 (easiest — kernel extras available via `apt`)
- Debian 12
- Arch Linux (with the `linux-headers` package)

## Kernel modules

redroid needs two kernel modules: `binder_linux` and `ashmem_linux`.

=== "Ubuntu / Debian"

    ```bash
    sudo apt install linux-modules-extra-$(uname -r)
    sudo modprobe binder_linux ashmem_linux
    ```

    To load them automatically at boot:

    ```bash
    echo -e 'binder_linux\nashmem_linux' | sudo tee /etc/modules-load.d/redroid.conf
    ```

=== "Arch Linux"

    ```bash
    # Install the binder and ashmem modules
    yay -S binder_linux-dkms ashmem-dkms
    sudo modprobe binder_linux ashmem_linux
    ```

Verify with `lsmod | grep -E 'binder|ashmem'` — both should appear.

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
    Beetroot's containers run with `privileged: true`. This is required for Android init and Magisk to function correctly.

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

If you want to use `beetroot frida` to attach Frida to processes, you need `frida-tools` on the host:

```bash
pip install frida-tools
# or, if you use uv's tool install:
uv tool install frida-tools
```

The Frida *server* binary is managed entirely by Beetroot — you don't need to install it separately.

## Summary checklist

- [ ] Linux host (physical or VM)
- [ ] `binder_linux` and `ashmem_linux` kernel modules loaded
- [ ] Docker Engine + Compose plugin installed; current user in `docker` group
- [ ] `uv` installed and on `PATH`
- [ ] `adb` on `PATH`
- [ ] (Optional) `frida` CLI available if you want `beetroot frida`

Once all boxes are checked, move on to [Installation](installation.md).

# Frida is bind-mounted per-instance from the host so each phone can pin
# its own version without rebuilding. The image only carries the boot-time
# scripts that configure Magisk and launch Frida; sqlite is provided by
# `magisk --sqlite` so we don't need to ship a separate binary.
ARG BASE_IMAGE=redroid/redroid:14.0.0_litegapps_houdini_magisk
FROM ${BASE_IMAGE}

# entrypoint.sh sources its sibling helpers (magisk-path.sh, magisk-config.sh,
# magisk-env.sh, flash-modules.sh, activate-zygisk.sh, launch-frida.sh) — the
# glob lands them all at filesystem root.
COPY --chmod=755 docker/*.sh /

# Android init parses every *.rc in /system/etc/init/ at boot.
COPY docker/stealth.rc /system/etc/init/stealth.rc

# No ENTRYPOINT — redroid's default /init runs as PID 1.
# stealth.rc fires /entrypoint.sh on sys.boot_completed=1.

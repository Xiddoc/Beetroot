"""
Bundled micro-VM build assets shipped inside the beetroot wheel.

Holds the three text artifacts the ``binder: vm`` guest build consumes —
``kernel.config`` (kernel-config fragment), ``guest-init.sh`` (guest
``/init``) and ``adbprobe.c`` (the guest ADB self-test). Resolved at
runtime via :func:`beetroot.paths.bundled_vm_dir`.
"""

"""beetroot — multi-instance Magisk-Android research lab CLI."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys

from . import compose, config, frida_dl, modules_dl, paths, ports, registry


def _ensure_exists(name: str) -> None:
    if registry.get(name) is None:
        sys.exit(f"error: no instance named {name!r}. Try `beetroot ls`.")


def _stage_instance(name: str, cfg: config.InstanceConfig) -> None:
    """Render .env and stage Frida + modules. Idempotent — safe on ``apply``."""
    meta = registry.get(name)
    assert meta is not None
    rendered_ports = ports.ports_for_index(meta["index"])

    paths.instance_data(name).mkdir(parents=True, exist_ok=True)
    paths.instance_modules(name).mkdir(parents=True, exist_ok=True)

    paths.instance_env(name).write_text(
        config.render_env(name, cfg, rendered_ports)
    )

    if cfg.frida is not None:
        frida_dl.stage_for_instance(name, cfg.frida.version)
    else:
        frida_dl.stage_empty(name)

    modules_dl.stage_for_instance(name, cfg)


# ---- verbs -----------------------------------------------------------------


def cmd_create(args: argparse.Namespace) -> None:
    """
    Create a new instance from a preset and stage its files.

    Args:
        args: Parsed CLI arguments (``name``, ``preset``, ``from_data``).
    """
    name = args.name
    if registry.get(name) is not None:
        sys.exit(f"error: instance {name!r} already exists.")

    cfg = config.load_preset(args.preset)
    paths.instance_dir(name).mkdir(parents=True, exist_ok=True)
    config.write_yaml(paths.instance_yaml(name), cfg)

    index = ports.lowest_free_index(registry.used_indices())
    registry.add(name, index)

    if args.from_data:
        src = (paths.repo_root() / args.from_data).resolve()
        if not src.is_dir():
            sys.exit(f"error: --from-data path {src} is not a directory.")
        dst = paths.instance_data(name)
        if dst.exists():
            shutil.rmtree(dst)
        print(f"[beetroot] copying {src} → {dst}")
        shutil.copytree(src, dst)

    _stage_instance(name, cfg)
    p = ports.ports_for_index(index)
    print(
        f"[beetroot] created {name} (index {index}, ADB localhost:{p['adb']}, "
        f"Frida localhost:{p['frida']})"
    )
    print(f"[beetroot] next: beetroot up {name}")


def cmd_apply(args: argparse.Namespace) -> None:
    """
    Re-render ``.env`` and re-stage files from the instance's beetroot.yaml.

    Args:
        args: Parsed CLI arguments (``name``).
    """
    _ensure_exists(args.name)
    cfg = config.load_instance(args.name)
    _stage_instance(args.name, cfg)
    print(f"[beetroot] re-staged {args.name} from beetroot.yaml")
    print(f"[beetroot] restart with: beetroot down {args.name} && beetroot up {args.name}")


def _resolve_names(args: argparse.Namespace) -> list[str]:
    """
    Return the list of instance names from ``--all`` or positional names.

    Raises sys.exit on conflicting or missing arguments.
    """
    if args.all and args.names:
        sys.exit("error: --all and explicit names are mutually exclusive.")
    if args.all:
        instances = registry.list_instances()
        if not instances:
            print("(no instances)")
            sys.exit(0)
        return sorted(instances)
    if not args.names:
        sys.exit("error: provide at least one instance name, or use --all.")
    return list(args.names)


def cmd_up(args: argparse.Namespace) -> None:
    """
    Start one or more instances.

    Args:
        args: Parsed CLI arguments (``names`` or ``--all``, ``build``).
    """
    for name in _resolve_names(args):
        _ensure_exists(name)
        compose.up(name, build=args.build)
        meta = registry.get(name)
        assert meta is not None
        p = ports.ports_for_index(meta["index"])
        print(f"[beetroot] {name} up — ADB localhost:{p['adb']}, Frida localhost:{p['frida']}")


def cmd_down(args: argparse.Namespace) -> None:
    """
    Stop one or more instances, preserving data.

    Args:
        args: Parsed CLI arguments (``names`` or ``--all``).
    """
    for name in _resolve_names(args):
        _ensure_exists(name)
        compose.down(name)
        print(f"[beetroot] {name} down (data preserved)")


def cmd_restart(args: argparse.Namespace) -> None:
    """
    Stop then start one or more instances.

    Args:
        args: Parsed CLI arguments (``names`` or ``--all``).
    """
    for name in _resolve_names(args):
        _ensure_exists(name)
        compose.down(name)
        compose.up(name)
        print(f"[beetroot] {name} restarted")


def cmd_destroy(args: argparse.Namespace) -> None:
    """
    Stop and permanently delete an instance including its data directory.

    Args:
        args: Parsed CLI arguments (``name``, ``yes``).
    """
    name = args.name
    _ensure_exists(name)
    if not args.yes:
        ans = input(f"Destroy {name} and delete instances/{name}/? [y/N] ").strip().lower()
        if ans != "y":
            print("[beetroot] aborted")
            return
    try:
        compose.down(name, volumes=True)
    except compose.ComposeError as e:
        print(f"[beetroot] (compose down failed: {e}; continuing)")
    instance_path = paths.instance_dir(name)
    if instance_path.exists():
        shutil.rmtree(instance_path)
    registry.remove(name)
    print(f"[beetroot] destroyed {name}")


def cmd_ls(args: argparse.Namespace) -> None:
    """
    List all registered instances with their ports and live status.

    Args:
        args: Parsed CLI arguments (``json``).
    """
    instances = registry.list_instances()
    if args.json:
        out = {}
        for name, meta in instances.items():
            p = ports.ports_for_index(meta["index"])
            out[name] = {
                "index": meta["index"],
                "adb": f"localhost:{p['adb']}",
                "frida": f"localhost:{p['frida']}",
                "status": compose.ps_status(name),
                "created_at": meta["created_at"],
            }
        print(json.dumps(out, indent=2, sort_keys=True))
        return

    if not instances:
        print("(no instances — try `beetroot create alpha`)")
        return
    print(f"{'NAME':<14}{'IDX':<5}{'ADB':<22}{'FRIDA':<22}{'STATUS':<14}")
    for name in sorted(instances):
        meta = instances[name]
        p = ports.ports_for_index(meta["index"])
        print(
            f"{name:<14}{meta['index']:<5}"
            f"{'localhost:' + str(p['adb']):<22}"
            f"{'localhost:' + str(p['frida']):<22}"
            f"{compose.ps_status(name):<14}"
        )


def cmd_logs(args: argparse.Namespace) -> None:
    """
    Tail container logs for an instance.

    Args:
        args: Parsed CLI arguments (``name``, ``follow``).
    """
    _ensure_exists(args.name)
    compose.logs(args.name, follow=args.follow)


def cmd_shell(args: argparse.Namespace) -> None:
    """
    Open an interactive ADB shell into an instance.

    Args:
        args: Parsed CLI arguments (``name``).
    """
    _ensure_exists(args.name)
    meta = registry.get(args.name)
    assert meta is not None
    p = ports.ports_for_index(meta["index"])
    if shutil.which("adb") is None:
        sys.exit("error: adb not found on PATH (install android-tools).")
    target = f"localhost:{p['adb']}"
    subprocess.run(["adb", "connect", target], check=False)
    subprocess.run(["adb", "-s", target, "shell"], check=False)


def cmd_env(args: argparse.Namespace) -> None:
    """
    Print eval-able ``ANDROID_DEVICE`` / ``FRIDA_DEVICE`` shell exports.

    Args:
        args: Parsed CLI arguments (``name``).
    """
    _ensure_exists(args.name)
    meta = registry.get(args.name)
    assert meta is not None
    p = ports.ports_for_index(meta["index"])
    print(f"export ANDROID_DEVICE=localhost:{p['adb']}")
    print(f"export FRIDA_DEVICE=localhost:{p['frida']}")


def cmd_frida(args: argparse.Namespace) -> None:
    """
    Invoke the frida CLI against an instance, forwarding extra arguments.

    Args:
        args: Parsed CLI arguments (``name``, ``frida_args``).
    """
    _ensure_exists(args.name)
    meta = registry.get(args.name)
    assert meta is not None
    p = ports.ports_for_index(meta["index"])
    if shutil.which("frida") is None:
        sys.exit("error: frida CLI not found (pip install frida-tools).")
    cmd = ["frida", "-H", f"localhost:{p['frida']}", *args.frida_args]
    subprocess.run(cmd, check=False)


def cmd_module(args: argparse.Namespace) -> None:
    """
    Append a module to beetroot.yaml and re-stage. Caller restarts.

    Args:
        args: Parsed CLI arguments (``name``, ``source``, ``sha256``).
    """
    _ensure_exists(args.name)
    cfg = config.load_instance(args.name)
    src = args.source
    sha256: str | None = args.sha256 or None
    if src.startswith(("http://", "https://")):
        cfg.modules.append(config.Module(url=src, sha256=sha256))
    else:
        cfg.modules.append(config.Module(path=src, sha256=sha256))
    config.write_yaml(paths.instance_yaml(args.name), cfg)
    modules_dl.stage_for_instance(args.name, cfg)
    print(f"[beetroot] added module → instances/{args.name}/beetroot.yaml")
    print(f"[beetroot] restart to flash: beetroot down {args.name} && beetroot up {args.name}")


# Mapping for the legacy single-instance layout: data/ was the original
# instance, data2/ + data3/ were ad-hoc copies. We assign them
# alpha/bravo/charlie at indices 0/1/2 so the canonical instance keeps
# its existing 5555 ADB port.
_LEGACY_MAPPING = [
    ("data", "alpha"),
    ("data2", "bravo"),
    ("data3", "charlie"),
]


def cmd_migrate(args: argparse.Namespace) -> None:
    """
    One-shot move from the legacy ``data/``, ``data2/``, ``data3/`` layout.

    Args:
        args: Parsed CLI arguments (``yes``).
    """
    if registry.list_instances():
        sys.exit("error: instances.json already has entries — refusing to migrate over them.")

    discovered = [
        (paths.repo_root() / src, name)
        for src, name in _LEGACY_MAPPING
        if (paths.repo_root() / src).is_dir()
    ]
    if not discovered:
        sys.exit(
            "error: no legacy data/, data2/, or data3/ found in "
            f"{paths.repo_root()} — nothing to migrate."
        )

    print("[beetroot] migrate plan:")
    for src, name in discovered:
        print(f"  {src.name:>6} → instances/{name}/data")
    if not args.yes:
        ans = input("Proceed? This MOVES the directories (no copy). [y/N] ").strip().lower()
        if ans != "y":
            print("[beetroot] aborted")
            return

    cfg = config.load_preset("default")
    for src, name in discovered:
        instance_path = paths.instance_dir(name)
        if instance_path.exists():
            sys.exit(f"error: {instance_path} already exists — clean it up before migrating.")
        instance_path.mkdir(parents=True)
        # Move (rename) — fast, no copy. Falls back to copy+remove across filesystems.
        src.rename(paths.instance_data(name))
        config.write_yaml(paths.instance_yaml(name), cfg)
        index = ports.lowest_free_index(registry.used_indices())
        registry.add(name, index)
        _stage_instance(name, cfg)
        p = ports.ports_for_index(index)
        print(
            f"[beetroot] migrated → {name} "
            f"(ADB localhost:{p['adb']}, Frida localhost:{p['frida']})"
        )

    print("[beetroot] done. Verify with: beetroot ls && beetroot up alpha bravo charlie")


# ---- argparse wiring -------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """
    Build and return the top-level ``beetroot`` argument parser.

    Returns:
        A fully configured ArgumentParser with all subcommands wired up.
    """
    p = argparse.ArgumentParser(prog="beetroot", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("create", help="create a new instance")
    s.add_argument("name")
    s.add_argument("--preset", default="default")
    s.add_argument("--from-data", help="copy an existing data dir as the instance's /data")
    s.set_defaults(func=cmd_create)

    s = sub.add_parser("apply", help="re-render .env and re-stage from beetroot.yaml")
    s.add_argument("name")
    s.set_defaults(func=cmd_apply)

    s = sub.add_parser("up", help="start one or more instances")
    s.add_argument("names", nargs="*")
    s.add_argument("--build", action="store_true", help="rebuild the image first")
    s.add_argument("--all", action="store_true", help="act on all registered instances")
    s.set_defaults(func=cmd_up)

    s = sub.add_parser("down", help="stop one or more instances (data preserved)")
    s.add_argument("names", nargs="*")
    s.add_argument("--all", action="store_true", help="act on all registered instances")
    s.set_defaults(func=cmd_down)

    s = sub.add_parser("restart", help="stop then start one or more instances (data preserved)")
    s.add_argument("names", nargs="*")
    s.add_argument("--all", action="store_true", help="act on all registered instances")
    s.set_defaults(func=cmd_restart)

    s = sub.add_parser("destroy", help="stop and delete an instance + its data")
    s.add_argument("name")
    s.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    s.set_defaults(func=cmd_destroy)

    s = sub.add_parser("ls", help="list instances")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser("logs", help="tail container logs")
    s.add_argument("name")
    s.add_argument("-f", "--follow", action="store_true")
    s.set_defaults(func=cmd_logs)

    s = sub.add_parser("shell", help="adb shell into an instance")
    s.add_argument("name")
    s.set_defaults(func=cmd_shell)

    s = sub.add_parser("env", help="print eval-able ANDROID_DEVICE / FRIDA_DEVICE exports")
    s.add_argument("name")
    s.set_defaults(func=cmd_env)

    s = sub.add_parser("frida", help="invoke frida against an instance (passes -H)")
    s.add_argument("name")
    s.add_argument("frida_args", nargs=argparse.REMAINDER)
    s.set_defaults(func=cmd_frida)

    s = sub.add_parser("module", help="add a Magisk module zip (URL or local path)")
    s.add_argument("name")
    s.add_argument("source", help="https URL or repo-relative path to a .zip")
    s.add_argument("--sha256", metavar="HEX", help="expected sha256 hex digest of the zip")
    s.set_defaults(func=cmd_module)

    s = sub.add_parser("migrate", help="one-shot: move legacy data/, data2/, data3/ → instances/")
    s.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    s.set_defaults(func=cmd_migrate)

    return p


def main() -> None:
    """Parse CLI arguments and dispatch to the appropriate subcommand handler."""
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

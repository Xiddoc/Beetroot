# Frida

Frida is **opt-in** starting in Beetroot v0.3. When you declare a `frida:` block in `beetroot.yaml`, the CLI downloads the correct architecture build from GitHub, caches it locally, and bind-mounts it into the container at `/data/local/tmp/frida-server`. At boot, `entrypoint.sh` launches it automatically.

If you omit the `frida:` block (the new default), the bind-mount is a 0-byte non-executable placeholder and `entrypoint.sh` skips the launch entirely — no `frida-server` process inside the container.

!!! note "Host-side `frida-tools` is also optional"
    The Frida server inside the container is managed for you when you opt in. The host-side `frida` CLI you attach with is a separate package and ships behind the `[frida]` extra — install it with `uv tool install 'beetroot[frida]'`. See [Installation › With Frida CLI](../getting-started/installation.md#with-frida-cli) for details. `beetroot frida-addr` itself only prints a port and needs nothing extra.

## Enabling Frida

The fastest way is to copy [`examples/with-frida.yaml`](examples.md) over a freshly-created instance's `beetroot.yaml`:

```bash
beetroot create alpha
cp examples/with-frida.yaml alpha/beetroot.yaml
beetroot apply alpha
```

`examples/with-frida.yaml` declares a pinned version for you. To enable Frida on an already-existing instance, edit its `beetroot.yaml` directly and add the block:

```yaml
frida:
  version: auto
```

Then run `beetroot apply <name>` to download and stage the binary, and restart the instance.

## Choosing a version

`frida.version` accepts three forms:

| Value | Behaviour |
|-------|-----------|
| `auto` (**default**) | Match the host's installed `frida-tools` version, so the staged server and the client you'll attach with agree on major+minor. Falls back to `latest` when `frida-tools` isn't installed. |
| `latest` | The current upstream release, resolved to a concrete tag at download time (via GitHub's latest-release redirect). |
| a pinned `major.minor.patch` (e.g. `"16.4.10"`) | That exact server, for reproducibility. **Required** if you also set `sha256`. |

```yaml
# Track your host frida-tools automatically (recommended):
frida:
  version: auto

# Or pin a specific server (reproducible):
frida:
  version: "16.4.10"
```

A malformed pin (`"16.4"`, `"16.4.10-rc1"`) fails at config-load. `auto`/`latest` resolve to a concrete tag at staging time, and the cache is keyed by that resolved tag.

You can optionally pin an expected `sha256` of the decompressed `frida-server` binary; if set, Beetroot verifies the cached binary against it (case-insensitive) and refuses to stage a mismatch — a guard against a hostile mirror. Because a digest can only match one specific build, `sha256` **requires a pinned `version`** (combining it with `auto`/`latest` is rejected at load).

Changing the version and running `beetroot apply <name>` re-downloads the binary into the instance directory at `frida-server`. The old binary is overwritten. Restart the instance to pick up the new server.

!!! tip "Keep versions in sync"
    Frida requires the client and server to agree on **major + minor**, or the connection fails. `version: auto` keeps them in lock-step automatically. If you pin a `version` (or use `latest`) that diverges from your host `frida-tools`, `beetroot apply` prints a one-line warning, because the `frida` client would otherwise fail to attach.

## Connecting Frida — `beetroot frida-addr`

Beetroot doesn't wrap the `frida` CLI. Wrapping it would mean opaque argument forwarding, which silently breaks frida's own shell completion, `--help`, and flag validation (see [issue #109](https://github.com/Xiddoc/Beetroot/issues/109)). Instead, `beetroot frida-addr` does the one thing the old wrapper actually saved you — resolving the instance's stride-allocated Frida port — and prints it to stdout, so you invoke native `frida` yourself:

```bash
beetroot frida-addr alpha
# → localhost:27042

frida -H "$(beetroot frida-addr alpha)" -n com.target.app
```

Because `frida` is the program you actually run, you keep its completion, `--help`, and flag validation. The pattern composes into any frida workflow:

```bash
ADDR="$(beetroot frida-addr alpha)"

# Attach to a process by name
frida -H "$ADDR" -n com.target.app

# Spawn and attach
frida -H "$ADDR" -f com.target.app --no-pause

# List running processes
frida -H "$ADDR" -ps

# Load a script
frida -H "$ADDR" -n com.target.app -l /path/to/script.js
```

!!! note "frida CLI required to attach"
    `beetroot frida-addr` only prints an address — it needs nothing extra. To actually attach you need the `frida` binary on your PATH; the easiest way to get it is `uv tool install 'beetroot[frida]'`, which bundles `frida-tools` alongside Beetroot. Alternatively, `uv tool install frida-tools` works standalone.

The same address is also available as the `frida_address` field of `beetroot status alpha` (JSON), or you can hardcode it from the [port table](../reference/ports.md) if you know the instance index.

## Using frida-tools Python API

```python
import frida

# Connect to instance at index 0 (port 27042)
device = frida.get_device_manager().add_remote_device("localhost:27042")
session = device.attach("com.target.app")
script = session.create_script("console.log('hello')")
script.load()
```

## Disabling Frida

Frida is off by default — `beetroot create` writes a minimal `beetroot.yaml` that doesn't ship a `frida:` block at all. If you turned it on and want to turn it back off, either delete the `frida:` block entirely or set it explicitly to null:

```yaml
frida: ~   # null in YAML
```

```bash
beetroot apply alpha
beetroot down alpha && beetroot up alpha
```

The `frida-server` bind-mount becomes a 0-byte non-executable placeholder, and `entrypoint.sh` skips the launch step (it checks `if it's executable` before starting).

## Troubleshooting

**`frida: command not found` when attaching.**
`beetroot frida-addr` only prints the address; you still need the `frida` client to attach. Install the `[frida]` extra: `uv tool install 'beetroot[frida]'` (or `uv tool install frida-tools` if Beetroot is already installed without the extra).

**Frida connects but can't enumerate processes.**
The server might still be starting. Wait a few seconds after boot, or check: `beetroot shell alpha` then `ps -A | grep frida`. If it's not running, check `beetroot logs alpha` for download or launch errors.

**Version mismatch error.**
`frida-tools` on the host and `frida.version` in `beetroot.yaml` must match on major + minor. Update one to match the other, then run `beetroot apply` and restart.

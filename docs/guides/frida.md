# Frida

Frida is **opt-in** starting in Beetroot v0.3. When you declare a `frida:` block in `beetroot.yaml`, the CLI downloads the correct architecture build from GitHub, caches it locally, and bind-mounts it into the container at `/data/local/tmp/frida-server`. At boot, `entrypoint.sh` launches it automatically.

If you omit the `frida:` block (the new default), the bind-mount is a 0-byte non-executable placeholder and `entrypoint.sh` skips the launch entirely — no `frida-server` process inside the container.

!!! note "Host-side `frida-tools` is also optional"
    The Frida server inside the container is managed for you when you opt in. The host-side `frida` CLI used by `beetroot frida` is a separate package and ships behind the `[frida]` extra — install it with `uv tool install 'beetroot[frida]'`. See [Installation › With Frida CLI](../getting-started/installation.md#with-frida-cli) for details.

## Enabling Frida

The fastest way is to copy [`examples/with-frida.yaml`](examples.md) over a freshly-created instance's `beetroot.yaml`:

```bash
beetroot create alpha
cp examples/with-frida.yaml alpha/beetroot.yaml
beetroot apply alpha
```

`examples/with-frida.yaml` declares the version-pin idiom for you. To enable Frida on an already-existing instance, edit its `beetroot.yaml` directly and add the block:

```yaml
frida:
  version: "16.4.10"
```

Then run `beetroot apply <name>` to download and stage the binary, and restart the instance.

## Version pinning

Each instance pins its own Frida version in `beetroot.yaml` (once you've opted in):

```yaml
frida:
  version: "16.4.10"
```

You can optionally pin an expected `sha256` of the decompressed `frida-server` binary; if set, Beetroot verifies the cached binary against it (case-insensitive) and refuses to stage a mismatch — a guard against a hostile mirror.

Changing the version and running `beetroot apply <name>` re-downloads the binary into the instance directory at `frida-server`. The old binary is overwritten. Restart the instance to pick up the new server.

!!! tip "Keep versions in sync"
    The Frida server version and your host-side `frida-tools` version must match (major + minor). A mismatch causes connection errors. Pin both explicitly in your research environment.

## `beetroot frida` wrapper

`beetroot frida` invokes the host-side `frida` CLI with `-H localhost:<frida_port>` pre-populated for the named instance:

```bash
beetroot frida alpha -- -n com.target.app -l hook.js
```

Everything after `--` is passed verbatim to `frida`. The `--` is optional but makes intent clear when `frida_args` start with `-`.

Examples:

```bash
# Attach to a process by name
beetroot frida alpha -n com.target.app

# Spawn and attach
beetroot frida alpha -f com.target.app --no-pause

# List running processes
beetroot frida alpha -ps

# Load a script
beetroot frida alpha -n com.target.app -l /path/to/script.js
```

!!! note "frida CLI required"
    `beetroot frida` shells out to the `frida` binary on your PATH. The easiest way to get it is `uv tool install 'beetroot[frida]'`, which bundles `frida-tools` alongside Beetroot. Alternatively, `uv tool install frida-tools` works standalone.

## Connecting without the wrapper

If you prefer to drive Frida directly, get the port from `beetroot status`:

```bash
FRIDA_DEVICE=$(beetroot status alpha | python3 -c "import json,sys; print(json.load(sys.stdin)['frida_address'])")
# $FRIDA_DEVICE = localhost:27042

frida -H "$FRIDA_DEVICE" -n com.target.app
```

Or hardcode from the [port table](../reference/ports.md) if you know the instance index.

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

**`beetroot frida` says `frida CLI not found`.**
Install the `[frida]` extra: `uv tool install 'beetroot[frida]'` (or `uv tool install frida-tools` if Beetroot is already installed without the extra).

**Frida connects but can't enumerate processes.**
The server might still be starting. Wait a few seconds after boot, or check: `beetroot shell alpha` then `ps -A | grep frida`. If it's not running, check `beetroot logs alpha` for download or launch errors.

**Version mismatch error.**
`frida-tools` on the host and `frida.version` in `beetroot.yaml` must match on major + minor. Update one to match the other, then run `beetroot apply` and restart.

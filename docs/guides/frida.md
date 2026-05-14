# Frida

Beetroot manages the Frida server binary per-instance: it downloads the correct architecture build from GitHub, caches it locally, and bind-mounts it into the container at `/data/local/tmp/frida-server`. At boot, `entrypoint.sh` launches it automatically.

## Version pinning

Each instance pins its own Frida version in `beetroot.yaml`:

```yaml
frida:
  version: "16.4.10"
```

Changing the version and running `beetroot apply <name>` re-downloads the binary into `instances/<name>/frida-server`. The old binary is overwritten. Restart the instance to pick up the new server.

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
    `beetroot frida` shells out to the `frida` binary on your PATH. Install it with `pip install frida-tools` or `uv tool install frida-tools`.

## Connecting without the wrapper

If you prefer to drive Frida directly, get the port from `beetroot env`:

```bash
eval $(beetroot env alpha)
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

Set `frida: null` in `beetroot.yaml` and apply:

```yaml
frida: ~   # null in YAML
```

```bash
beetroot apply alpha
beetroot down alpha && beetroot up alpha
```

The `frida-server` bind-mount becomes an empty placeholder, and `entrypoint.sh` skips the launch step (it checks `if it's executable` before starting).

## Troubleshooting

**`beetroot frida` says `frida CLI not found`.**
Install `frida-tools`: `pip install frida-tools`.

**Frida connects but can't enumerate processes.**
The server might still be starting. Wait a few seconds after boot, or check: `beetroot shell alpha` then `ps -A | grep frida`. If it's not running, check `beetroot logs alpha` for download or launch errors.

**Version mismatch error.**
`frida-tools` on the host and `frida.version` in `beetroot.yaml` must match on major + minor. Update one to match the other, then run `beetroot apply` and restart.

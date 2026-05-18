# Troubleshooting

Common problems and their solutions. If your issue isn't listed here, check `beetroot logs <name>` first — the entrypoint output is usually informative.

---

## `docker compose up` fails with binder / ashmem errors

**Symptom:** Container exits immediately with kernel errors mentioning `binder` or `ashmem`.

**Cause:** Your host kernel is missing the modules redroid requires.

**Fix:**

=== "Ubuntu / Debian"

    ```bash
    sudo apt install linux-modules-extra-$(uname -r)
    sudo modprobe binder_linux ashmem_linux
    ```

=== "Arch Linux"

    ```bash
    yay -S binder_linux-dkms ashmem-dkms
    sudo modprobe binder_linux ashmem_linux
    ```

Verify: `lsmod | grep -E 'binder|ashmem'` should show both modules.

---

## `adb connect` succeeds but `adb shell` hangs

**Symptom:** ADB connects but the shell prompt never appears.

**Cause:** First boot takes 30–60 seconds while Android initializes. The device isn't ready yet.

**Fix:** Watch the logs:

```bash
beetroot logs alpha -f
```

Wait for:

```
[*] Android boot detected. Applying Beetroot configuration...
```

Once you see that line (and the following Magisk + Zygisk steps), `adb shell` will work.

---

## Magisk shows installed but Zygisk / denylist is off

**Symptom:** `magisk --sqlite 'SELECT value FROM settings WHERE key="zygisk"'` returns 0.

**Cause:** `entrypoint.sh` writes the DB settings only after `/data/adb/magisk.db` exists. If you mounted a `data/` from a Magisk-less image, or the DB was created after the script already ran, the writes were skipped.

**Fix:** Destroy and recreate the instance to get a fresh `data/`:

```bash
beetroot destroy -y alpha
beetroot create alpha
cp examples/stealth.yaml alpha/beetroot.yaml
beetroot apply alpha
beetroot up alpha
```

---

## Frida can't see processes

**Step 0:** Confirm Frida is enabled for this instance. Frida is opt-in starting in v0.3 — if `beetroot.yaml` has no `frida:` block, the staged binary is a 0-byte placeholder and `entrypoint.sh` skips the launch. Add a `frida: {version: "16.4.10"}` block (or copy `examples/with-frida.yaml` over the file) and re-`apply`.

**Step 1:** Confirm the binary is staged (use `beetroot ls --json` to get the path):

```bash
ls -lh "$(beetroot ls --json | jq -r .alpha.path)/frida-server"
# Should be ~10 MB and executable (mode 755 or similar) when Frida is enabled;
# 0 bytes / not-executable when the `frida:` block is omitted.
```

**Step 2:** Confirm it's running inside the container:

```bash
beetroot shell alpha
ps -A | grep frida
```

**Step 3:** Check for launch errors:

```bash
beetroot logs alpha | grep -i frida
```

**Common causes:**

- Frida server not staged: run `beetroot apply alpha` to re-download and re-stage.
- Version mismatch: `frida-tools` on the host and `frida.version` in `beetroot.yaml` must match on major + minor. Update one to match the other.
- Port conflict: another process on the host is using port 27042. Check with `ss -tlnp | grep 27042`.

---

## `beetroot frida` exits with "frida CLI not found"

**Symptom:** Running `beetroot frida <name>` fails immediately with `error: frida CLI not found. Install via uv tool install 'beetroot[frida]' or uv tool install frida-tools.`

**Cause:** The host-side `frida` CLI is optional and isn't included in a plain `uv tool install`. The `beetroot frida` verb shells out to whatever `frida` binary is on your PATH, and finds none.

**Fix:** Reinstall with the `[frida]` extra so `frida-tools` is bundled alongside Beetroot:

```bash
uv tool install --force 'beetroot[frida]'
```

Alternatively, install `frida-tools` on its own without disturbing the existing Beetroot install:

```bash
uv tool install frida-tools
```

---

## `beetroot module` added a zip but it didn't flash

**Cause:** `entrypoint.sh` only iterates `/flash_dir` once, at boot time. Adding a module after boot doesn't flash it automatically.

**Fix:** Restart:

```bash
beetroot down alpha && beetroot up alpha
```

---

## Instance stuck in "exited" state after host reboot

**Cause:** Docker containers don't automatically restart unless configured with `restart: always`. Beetroot doesn't set a restart policy by design — instances should be started explicitly.

**Fix:** Simply start the instance again:

```bash
beetroot up alpha
```

Your data is intact in the instance directory's `data/`.

---

## `beetroot apply` fails with sha256 mismatch

**Symptom:**

```
error: module sha256 mismatch for Module.zip
  expected: abc123...
  got:      def456...
```

**Cause:** The remote file changed since you pinned the hash, or the URL now points to a different file.

**Fix:** Either:

1. Remove the `sha256:` field from `beetroot.yaml` (accepts any file), or
2. Re-download manually, verify you trust the new file, compute the new hash, and update `beetroot.yaml`.

---

## Docker out of disk space

Android `/data` grows over time. Check (replace `<path>` with the value from `beetroot ls`):

```bash
beetroot ls --json | jq -r '.[].path' | xargs -I{} du -sh {}/data
```

To reclaim space from a destroyed instance (Docker might still hold volume space):

```bash
docker system prune --volumes
```

!!! warning "prune removes all unused volumes"
    `docker system prune --volumes` removes **all** Docker volumes not currently in use, not just Beetroot's. Use with care if you have other Docker projects on the same host.

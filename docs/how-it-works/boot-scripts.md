# Boot Scripts

The boot trigger and overall sequence are documented in
[Boot Flow](boot-flow.md). This page documents the **helper shell
scripts** that `entrypoint.sh` sources at boot.

`stealth.rc` is **out of scope** for this page — that file is Android init
syntax (not POSIX sh), and the only thing it does is fire
`entrypoint.sh` once `sys.boot_completed=1` is set. See
[Boot Flow → How `entrypoint.sh` gets invoked](boot-flow.md#how-entrypointsh-gets-invoked)
for the init trigger.

## Why split entrypoint.sh

v0.2 shipped a single 40-line `entrypoint.sh`. v0.3 splits it into
three helpers plus a 12-line glue file. The split delivers two things:

- **Per-helper contracts.** Each helper has one job, one set of env
  vars, and one place in the boot sequence. New behaviour (e.g.
  v0.4's path randomization) lands in one file, not the entire
  entrypoint.
- **Env-var-driven container paths.** Every container-side path the
  helpers touch is read from a `BEETROOT_*` env var with a safe
  default. v0.4's [stealth-posture work](../design/stealth-posture.md)
  randomizes those paths per-build by setting the env vars at compose
  time — no helper code changes.

## Shared contract

All three helpers obey the same rules:

- **Shell:** `/system/bin/sh` (Android's toybox-derived shell). Strict
  POSIX sh. No bashisms (`[[ ]]`, arrays, process substitution),
  no GNU coreutils flags.
- **Path env vars:** every container-side path read from an env var
  with `${VAR:-/safe-default}` POSIX defaulting. No path is
  hard-coded.
- **Logging:** `echo "[*] <action>"` for progress, `echo "[!] <warning>"`
  for non-fatal anomalies. Errors that should abort the boot use
  `echo "[!] ... " >&2; exit 1` — but in practice the helpers are
  resilient enough to never need this in v0.3.
- **Exit semantics:** exit 0 on success. The helpers are
  idempotent — Android init may re-trigger the entrypoint, so each
  helper must be safe to run multiple times against the same DB / FS
  state.
- **Linting:** `shellcheck -s sh docker/*.sh` is enforced by CI. New
  shellcheck findings block merge.

## `entrypoint.sh`

The glue. Prints a boot banner, sources the three helpers below in
order, then `wait`s so the shell stays alive as the parent of any
backgrounded children (notably `frida-server`).

| Item        | Value                                                       |
|-------------|-------------------------------------------------------------|
| Path        | `/entrypoint.sh` (in container)                             |
| Triggered by| `stealth.rc` on `sys.boot_completed=1`                      |
| SELinux ctx | `u:r:magisk:s0` (from `exec_background` in `stealth.rc`)    |
| Env vars    | none directly; helpers read their own                       |
| Idempotent  | yes (helpers are; the glue does nothing else)               |

## `magisk-config.sh`

Writes Beetroot's stealth posture into Magisk's sqlite DB. Waits for
the Magisk daemon (`magisk --sqlite "SELECT 1"`) before any writes;
without this wait, the writes race the daemon's own DB initialisation
and silently no-op. The wait is **bounded** (default 120 one-second
attempts, ~2 minutes — conservative because a first boot of
redroid+Magisk can legitimately take a while): if the daemon never
answers, the helper prints a `[!]` error to stderr and exits 1, which
aborts the sourced-under-`set -eu` entrypoint so the failure surfaces
in `docker compose logs` / `beetroot doctor` instead of hanging the
boot configuration forever in a container that looks "up".

Four actions (v0.4 T2):

1. `REPLACE INTO settings VALUES ('zygisk', 1)` — enable Zygisk.
2. `REPLACE INTO settings VALUES ('denylist', 1)` — enable denylist.
3. `SELECT value FROM settings WHERE key='zygisk'` — verify the write
   landed and exit non-zero if Magisk returned anything other than `1`
   (post-write verification added in v0.4 to catch silent regressions
   when Magisk's schema or daemon-race timing changes).
4. `INSERT OR IGNORE INTO denylist` — enrol each package in
   `BEETROOT_DENYLIST_PACKAGES` (comma-separated). The list is the
   string form of `stealth.denylist` from `beetroot.yaml`; per-package
   shape is validated by the pydantic regex in `Stealth._check_packages`,
   so the helper joins on `,` without escaping.

| Env var                       | Default                                                 | Notes                                                                                                                          |
|-------------------------------|---------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------|
| `BEETROOT_MAGISK_DB`          | `/data/adb/magisk.db`                                   | Informational only — `magisk --sqlite` always targets this path internally. Echoed in the waiting-log line.                    |
| `BEETROOT_DENYLIST_PACKAGES`  | `com.google.android.gms,com.google.android.gms.unstable` | Comma-separated package ids. Empty list → helper skips the INSERT entirely (no SQL'inject of an empty `('', '')` row).         |
| `BEETROOT_MAGISK_WAIT_SECS`   | `120`                                                   | Upper bound (1-second probe attempts) on the daemon wait; on timeout the helper exits 1. Script-level knob only — not passed through `compose.yaml` / `render_env`. |

**Idempotency:** `REPLACE INTO` and `INSERT OR IGNORE` both no-op on
re-run.

## `flash-modules.sh`

Iterates `*.zip` in the modules directory and calls
`magisk --install-module` on each. The host CLI mirrors
`beetroot.yaml`'s `modules:` list into this directory on `apply`.

| Env var                 | Default                      | Notes                                                                                                                                                                  |
|-------------------------|------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `BEETROOT_MODULES_DIR`  | `/data/adb/modules_update`   | Container path where zips are staged. v0.4 T4 (§3.5 of the stealth-posture design) replaced the Beetroot-invented `/flash_dir` with Magisk's well-known staging dir. |

If the directory is missing, the helper prints `[!] Modules directory
$DIR not present — skipping flash step.` and exits 0 (a missing modules
dir is a config choice, not an error).

A failing `magisk --install-module` is **non-fatal**: the helper prints
`[!] Module $zip failed to install — continuing.` and moves on to the
next zip. The helper is sourced under the entrypoint's `set -e`, so a
bare non-zero exit would otherwise abort the whole boot and skip
`launch-frida.sh` — one bad module must not take the container down.

**Idempotency:** Magisk's `--install-module` handles re-install of an
already-present module gracefully.

## `launch-frida.sh`

Checks the executable bit on the frida-server binary and, if set,
launches it in the background as a child of the entrypoint shell.

| Env var              | Default                            | Notes                                                                                                  |
|----------------------|------------------------------------|--------------------------------------------------------------------------------------------------------|
| `BEETROOT_FRIDA_BIN` | `/data/local/tmp/frida-server`     | Container path to frida-server. v0.4 (§3.1 of the stealth-posture design) randomizes this per build.   |

The host CLI is responsible for staging the binary at this path
(decompress from the GitHub release into `<instance-dir>/frida-server`,
which compose bind-mounts here read-only).

If the binary is missing or not executable, the helper prints two `[!]`
lines explaining what to do, and exits 0 — the container still boots
and serves ADB, the researcher can stage Frida later and `restart`.

**Idempotency:** init only runs the entrypoint once per boot, so we
don't guard against double-launch (which would fail on the second
attempt's port bind anyway).

## Env-var contract summary

| Env var                | Default                              | Consumer            |
|------------------------|--------------------------------------|---------------------|
| `BEETROOT_MAGISK_DB`   | `/data/adb/magisk.db`                | `magisk-config.sh`  |
| `BEETROOT_MODULES_DIR` | `/data/adb/modules_update`           | `flash-modules.sh`  |
| `BEETROOT_FRIDA_BIN`   | `/data/local/tmp/frida-server`       | `launch-frida.sh`   |

All three are passed through `compose.yaml`'s service `environment:`
block from the host shell, with an empty-string fallback that triggers
each helper's bake-in default. (`BEETROOT_MAGISK_WAIT_SECS` is
intentionally absent from this table — it never crosses compose; it's
a script-level default consumed only by `magisk-config.sh`, see above.)

## Why not Python?

Three reasons the helpers stay in POSIX sh:

- **Image bloat.** Adding a Python runtime to the redroid image costs
  ~30 MB. The helpers do roughly nothing — wait on a daemon, run a
  handful of sqlite writes, exec a binary — and don't justify the
  cost.
- **Init context.** `entrypoint.sh` runs under
  `u:r:magisk:s0` (Android SELinux context). Python's startup
  performs `dlopen`s and `connect`s that aren't part of the audited
  Magisk surface; staying in sh keeps the boot path simple.
- **`stealth.rc` can't be Python anyway.** Android init parses
  `.rc` files natively. The init trigger has to be Android-init
  syntax regardless, so the rest of the chain stays in the same
  shell-native idiom.

## Modifying the helpers

When editing any of these files:

- Run `shellcheck -s sh docker/*.sh` locally before commit. CI
  enforces a clean run.
- Run `grep -E '/data/local/tmp/frida-server|/data/adb/modules_update|/data/adb/magisk.db' docker/*.sh`
  — every match should be inside a `${VAR:-/default}` expansion (or a
  doc comment). Hard-coded paths break v0.4's randomization design.
- The Magisk DB schema is load-bearing; do not rewrite the SQL
  statements without a corresponding change to Magisk's own
  expectations.

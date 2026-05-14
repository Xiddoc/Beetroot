# Changelog

## Unreleased

### Added
- `android.gapps` field: choose `none | lite | full | mindthegapps` per instance.
  `lite` is the default and preserves current behavior.
- `scripts/setup.sh <variant>`: produce the corresponding redroid base image.
- New preset: `no-gapps`.
- `src/beetroot/settings.py` — `Settings(BaseSettings)` for environment-driven
  overrides. Set `BEETROOT_DOCKER_BIN`, `BEETROOT_FRIDA_ARCH`, or
  `BEETROOT_HTTP_TIMEOUT` to override the defaults (`docker`,
  `android-x86_64`, `30`). Useful for ARM-based Android VMs, slow networks,
  or non-standard docker binary locations.

### Changed
- Mypy is now run with `strict = true` and the `pydantic.mypy` plugin. CI
  catches a significantly wider class of type errors, including incorrect
  pydantic constructor calls and unnarrowed `Optional` accesses.

### Breaking: `android.base_image` removed

`android.base_image` is no longer a valid field in `beetroot.yaml`.  Replace it
with `android.version` (an integer: `11`, `12`, `13`, or `14`):

```yaml
# Before
android:
  base_image: redroid/redroid:14.0.0_litegapps_houdini_magisk

# After
android:
  version: 14
```

The image tag is now derived automatically by the CLI (`config.base_image_tag()`).
Loading a YAML that still contains `android.base_image` raises a `ValueError` with
this migration message.  All shipped presets have been updated.

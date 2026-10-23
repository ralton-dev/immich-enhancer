"""Settings, read once from the environment.

`IMMICH_URL`, `IMMICH_API_KEY` and `APP_VERSION` are named outright. Every other
field is `ENHANCER_` plus the field name in capitals: `sr_model` is
`ENHANCER_SR_MODEL`. The README's configuration table is the spec for names,
defaults and which value is secret.

`Settings.from_env()` checks everything before the job touches Immich and raises
one `ConfigError` naming every variable that is missing or invalid. Booleans are
on only for the exact string `true`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import MISSING, dataclass, field, fields

log = logging.getLogger(__name__)

# The upscalers baked into the image (plan decision 8). The weight file is
# `<model_dir>/<name>.pth`.
SR_MODELS = ("RealESRGAN_x4plus", "realesr-general-x4v3")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

ENV_PREFIX = "ENHANCER_"
_NAMED_OUTRIGHT = {
    "immich_url": "IMMICH_URL",
    "immich_api_key": "IMMICH_API_KEY",
    "app_version": "APP_VERSION",
}

# Inclusive bounds for the integer settings. A tile below 64 px spends most of
# its work on the 16 px overlap the tiler adds on every side.
_INT_BOUNDS = {
    "max_per_run": (1, None),
    "time_budget_s": (1, None),
    "max_output_mp": (1, None),
    "tile": (64, None),
    "threads": (1, None),
    "jpeg_quality": (1, 100),
}


class ConfigError(ValueError):
    """A setting is missing or invalid. The message names each variable."""


def env_name(field_name: str) -> str:
    """The environment variable that sets `field_name`."""
    return _NAMED_OUTRIGHT.get(field_name, ENV_PREFIX + field_name.upper())


@dataclass(frozen=True, kw_only=True)
class Settings:
    immich_url: str = "http://immich-server.immich.svc.cluster.local:2283"
    immich_api_key: str = field(repr=False)  # required, secret: never logged
    queue_tag: str = "enhance"
    done_tag: str = "enhanced"
    pixel_tag: str = "pixel-x4"
    source_tag: str = "enhanced-source"
    failed_tag: str = "enhance-failed"
    skipped_tag: str = "enhance-skipped"
    max_per_run: int = 10
    time_budget_s: int = 2700
    max_output_mp: int = 16
    sr_model: str = "RealESRGAN_x4plus"
    tile: int = 256
    threads: int = 4
    model_dir: str = "/opt/models"
    work_dir: str = "/tmp/enhancer"
    jpeg_quality: int = 92
    dry_run: bool = False
    app_version: str  # required
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Build from `environ` (default `os.environ`), or raise `ConfigError`."""
        env = os.environ if environ is None else environ
        values: dict[str, object] = {}
        errors: list[str] = []
        for f in fields(cls):
            name = env_name(f.name)
            raw = env.get(name)
            if raw is None:
                if f.default is MISSING:
                    errors.append(f"missing required environment variable {name}")
                continue
            try:
                values[f.name] = _parse(f.name, f.type, raw)
            except ValueError as exc:
                errors.append(f"{name}: {exc}")

        known = {env_name(f.name) for f in fields(cls)}
        unknown = sorted(k for k in env if k.startswith(ENV_PREFIX) and k not in known)
        if unknown:
            # A typo in a ConfigMap would otherwise silently keep the default.
            log.warning("ignoring unknown setting(s): %s", ", ".join(unknown))

        if errors:
            raise ConfigError("invalid configuration: " + "; ".join(errors))
        return cls(**values)


def _parse(field_name: str, kind: str, raw: str) -> object:
    """One value from its string form. The API key's only possible error is
    "set but empty", so no message can echo it."""
    if kind == "bool":
        return raw == "true"
    if kind == "int":
        try:
            value = int(raw)
        except ValueError:
            raise ValueError(f"must be an integer, got {raw!r}") from None
        low, high = _INT_BOUNDS.get(field_name, (None, None))
        if (low is not None and value < low) or (high is not None and value > high):
            bounds = f"between {low} and {high}" if high is not None else f"at least {low}"
            raise ValueError(f"must be {bounds}, got {value}")
        return value
    # Stripped: a key sealed from `echo key | kubeseal` ends in a newline,
    # which is not a valid header value.
    value = raw.strip()
    if not value:
        raise ValueError("is set but empty")
    if field_name == "immich_url":
        return value.rstrip("/")
    if field_name == "sr_model" and value not in SR_MODELS:
        raise ValueError(f"must be one of {', '.join(SR_MODELS)}, got {value!r}")
    if field_name == "log_level":
        if value.upper() not in LOG_LEVELS:
            raise ValueError(f"must be one of {', '.join(LOG_LEVELS)}, got {value!r}")
        return value.upper()
    return value

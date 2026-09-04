"""Process-wide configuration: the registry, and the optional sample sidecar.

The registry is **mounted, not baked in**. It was a file in the image while this
server lived in the monorepo next to the dbt project that defines it, which made
a definition change an image build; here the image is generic and the definitions
belong to whichever deployment owns them. So all three files arrive as a
ConfigMap and the image carries none of them. `SEMANTIC_REGISTRY_VERSION` is
still stamped and still travels on every answer - it now identifies the mounted
registry rather than the image.

Loaded once at first use and cached, so the invalidation question is unchanged:
a definition change is a ConfigMap change plus a restart, not a live reload.

The three files differ in what absence means, and the difference is deliberate:

- registry and manifest: **fatal**. Table names are resolved from the manifest,
  so without it every answer would name a table that may not exist. A server
  that cannot answer correctly must not answer.
- `dimension_samples.json`: **optional**. It needs a live warehouse to produce,
  so it is refreshed cluster-side on its own cadence; its absence degrades
  `list_dimensions` to names-only and fails nothing.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path

from .registry import Registry, bind_tables
from .registry import load as load_registry

logger = logging.getLogger(__name__)

# Outside the code tree, and that is load-bearing rather than tidy. `/app` is
# WORKDIR and lands on `sys.path` for `python -m`, so a mount at `/app/semantic/`
# shadows the `semantic` package itself and the server dies at startup with
# "module 'semantic' has no attribute 'register'" - the same collision also
# breaks pytest collection. The monorepo dodged it by naming the directory
# `semantic_registry`; `/etc/mcp/<server>/` cannot collide at all.
DEFAULT_CONFIG_DIR = "/etc/mcp/semantic"

_DEFAULT_REGISTRY = f"{DEFAULT_CONFIG_DIR}/registry.yaml"
_DEFAULT_MANIFEST = f"{DEFAULT_CONFIG_DIR}/manifest.json"
_DEFAULT_SAMPLES = f"{DEFAULT_CONFIG_DIR}/dimension_samples.json"


def registry_path() -> Path:
    return Path(os.environ.get("SEMANTIC_REGISTRY_PATH", _DEFAULT_REGISTRY))


def registry_version() -> str:
    return os.environ.get("SEMANTIC_REGISTRY_VERSION", "unknown")


@lru_cache(maxsize=1)
def registry() -> Registry:
    """The registry, loaded and validated once.

    Validation runs here as well as in CI: the same rules that fail a PR fail
    the server's boot, so a registry that reaches the cluster has already passed
    the gate a PR would have applied.
    """
    path = registry_path()
    manifest_path = Path(os.environ.get("SEMANTIC_MANIFEST_PATH", _DEFAULT_MANIFEST))
    manifest = _read_json(manifest_path)
    if manifest is None:
        raise FileNotFoundError(
            f"no dbt manifest at {manifest_path}: table names are resolved from it, so the "
            f"registry cannot be bound without it"
        )
    loaded = load_registry(path, version=registry_version(), manifest=manifest)
    bind_tables(loaded, manifest)
    unbound = [model.name for model in loaded.models.values() if not model.table]
    if unbound:
        raise ValueError(f"semantic_models with no table in the manifest: {', '.join(unbound)}")
    logger.info(
        "registry loaded version=%s metrics=%d models=%d",
        loaded.version,
        len(loaded.metrics),
        len(loaded.models),
    )
    return loaded


@lru_cache(maxsize=1)
def _samples() -> dict:
    path = Path(os.environ.get("SEMANTIC_SAMPLES_PATH", _DEFAULT_SAMPLES))
    data = _read_json(path)
    if data is None:
        logger.info("no dimension samples at %s; list_dimensions degrades to names only", path)
        return {}
    return data


def samples(model: str) -> dict:
    """Per-dimension `{cardinality, values}` for one model, or `{}` if unrefreshed."""
    return (_samples().get(model) or {}) if model else {}


def sample_values(model: str) -> dict[str, list[str]]:
    """Just the known values per dimension, for near-miss filter validation."""
    return {
        name: detail.get("values", [])
        for name, detail in samples(model).items()
        if detail.get("values")
    }


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None

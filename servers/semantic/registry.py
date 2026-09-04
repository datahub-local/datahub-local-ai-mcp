"""The registry: metric and dimension definitions, loaded from YAML.

The registry is the *only* place a SQL fragment may come from. Everything the
compiler emits is either a literal it wrote itself or a string read from here,
which is reviewed YAML in git - never a value from a request. That split is what
makes an injected predicate inexpressible rather than merely filtered.

Validation lives in `validate_registry`, which is shared by the CI gate and by
process start: the same rules that fail a PR fail the server's boot, so a
registry that reaches the cluster has already passed the gate that a PR would.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml

GRAINS = ("day", "week", "month", "quarter", "year")
_GRAIN_ORDER = {grain: index for index, grain in enumerate(GRAINS)}

# Aggregations that are wrong over a column that is already a SUM. Averaging a
# pre-aggregated total answers a question nobody asked - the mean of daily
# category totals is not the mean of anything a person would name.
_AGG_BANNED_ON_PRE_AGGREGATED = frozenset({"avg", "min", "max"})
_AGGS = frozenset({"sum", "count", "count_distinct", "avg", "min", "max"})


class RegistryError(Exception):
    """A registry that must not load. Raised with every problem found, not the first."""


@dataclass(frozen=True)
class Measure:
    name: str
    agg: str
    expr: str
    model: str

    def render(self) -> str:
        if self.agg == "count_distinct":
            return f"count(DISTINCT {self.expr})"
        return f"{self.agg}({self.expr})"


@dataclass(frozen=True)
class Dimension:
    name: str
    type: str
    expr: str
    granularity: str | None = None

    @property
    def is_time(self) -> bool:
        return self.type == "time"


@dataclass(frozen=True)
class Entity:
    name: str
    type: str
    expr: str


@dataclass(frozen=True)
class SemanticModel:
    name: str
    description: str
    ref: str
    table: str
    time_dimension: str
    pre_aggregated: bool
    entities: tuple[Entity, ...]
    dimensions: dict[str, Dimension]
    measures: dict[str, Measure]

    def dimension(self, name: str) -> Dimension | None:
        return self.dimensions.get(name)


@dataclass(frozen=True)
class Metric:
    name: str
    label: str
    type: str
    model: str
    description: str
    excludes: str
    owner: str
    grain_min: str
    measure: str | None = None
    numerator: str | None = None
    denominator: str | None = None

    @property
    def measures(self) -> tuple[str, ...]:
        if self.type == "ratio":
            return (self.numerator or "", self.denominator or "")
        return (self.measure or "",)


@dataclass
class Registry:
    models: dict[str, SemanticModel] = field(default_factory=dict)
    metrics: dict[str, Metric] = field(default_factory=dict)
    version: str = "unknown"

    def metric(self, name: str) -> Metric:
        try:
            return self.metrics[name]
        except KeyError:
            raise KeyError(f"no metric named {name!r}") from None

    def model_for(self, metric: Metric) -> SemanticModel:
        return self.models[metric.model]

    def dimensions_for(self, metric: Metric) -> dict[str, Dimension]:
        return self.models[metric.model].dimensions

    def measure(self, metric: Metric, name: str) -> Measure:
        return self.models[metric.model].measures[name]


def grain_at_least(grain: str, minimum: str) -> bool:
    """Whether ``grain`` is no finer than ``minimum``."""
    return _GRAIN_ORDER[grain] >= _GRAIN_ORDER[minimum]


def load(path, *, version: str = "unknown", manifest: dict[str, Any] | None = None) -> Registry:
    """Parse, validate and return the registry. Raises `RegistryError` on any problem."""
    raw = yaml.safe_load(_read(path)) or {}
    registry = _build(raw)
    registry.version = version
    problems = validate_registry(registry, raw, manifest)
    if problems:
        raise RegistryError("\n".join(problems))
    return registry


def _read(path) -> str:
    if hasattr(path, "read_text"):
        return path.read_text()
    return open(path).read()


def _ref_name(ref: str) -> str:
    """`ref('invoices')` -> `invoices`. The registry never names a table directly."""
    inner = ref.strip()
    if not (inner.startswith("ref(") and inner.endswith(")")):
        raise RegistryError(f"model must be a ref() call, got {ref!r}")
    return inner[4:-1].strip().strip("\"'")


def _build(raw: dict[str, Any]) -> Registry:
    registry = Registry()

    for entry in raw.get("semantic_models") or []:
        name = entry.get("name")
        if not name:
            raise RegistryError("a semantic_model has no name")
        ref = _ref_name(entry.get("model", ""))

        dimensions: dict[str, Dimension] = {}
        for item in entry.get("dimensions") or []:
            dim_name = item["name"]
            dimensions[dim_name] = Dimension(
                name=dim_name,
                type=item.get("type", "categorical"),
                expr=item.get("expr", dim_name),
                granularity=(item.get("type_params") or {}).get("time_granularity"),
            )

        measures: dict[str, Measure] = {}
        for item in entry.get("measures") or []:
            measures[item["name"]] = Measure(
                name=item["name"],
                agg=item.get("agg", "sum"),
                expr=item.get("expr", item["name"]),
                model=name,
            )

        entities = tuple(
            Entity(name=item["name"], type=item.get("type", "foreign"), expr=item.get("expr", item["name"]))
            for item in entry.get("entities") or []
        )

        registry.models[name] = SemanticModel(
            name=name,
            description=(entry.get("description") or "").strip(),
            ref=ref,
            # Filled from the dbt manifest by `bind_tables`; never written by hand,
            # so a model cannot point at a table dbt does not build.
            table="",
            time_dimension=(entry.get("defaults") or {}).get("agg_time_dimension", ""),
            pre_aggregated=bool(entry.get("pre_aggregated")),
            entities=entities,
            dimensions=dimensions,
            measures=measures,
        )

    for entry in raw.get("metrics") or []:
        name = entry.get("name")
        if not name:
            raise RegistryError("a metric has no name")
        params = entry.get("type_params") or {}
        meta = entry.get("meta") or {}
        metric_type = entry.get("type", "simple")

        measure = params.get("measure")
        numerator = params.get("numerator")
        denominator = params.get("denominator")
        owning = _owning_model(registry, [measure] if metric_type != "ratio" else [numerator, denominator])

        registry.metrics[name] = Metric(
            name=name,
            label=entry.get("label", name),
            type=metric_type,
            model=owning,
            description=(meta.get("description") or "").strip(),
            excludes=(meta.get("excludes") or "").strip(),
            owner=meta.get("owner", ""),
            grain_min=meta.get("grain_min", "day"),
            measure=measure,
            numerator=numerator,
            denominator=denominator,
        )

    return registry


def _owning_model(registry: Registry, measures: list[str | None]) -> str:
    """The single model carrying every measure a metric names.

    A metric spanning two models needs a declared join, which v1 does not have,
    so this returns "" and validation reports it by name rather than the
    compiler guessing a join at query time.
    """
    owners = {
        model.name
        for model in registry.models.values()
        for measure in measures
        if measure and measure in model.measures
    }
    return owners.pop() if len(owners) == 1 else ""


def bind_tables(registry: Registry, manifest: dict[str, Any]) -> None:
    """Resolve each model's `ref()` to the table dbt builds, in place.

    The fully-qualified name comes from the manifest rather than from the YAML,
    so a catalog or schema move follows dbt without a registry edit and a ref
    that dbt does not build cannot resolve to a table name at all.
    """
    by_name = {
        node["name"]: node
        for node in (manifest.get("nodes") or {}).values()
        if node.get("resource_type") == "model"
    }
    for name, model in list(registry.models.items()):
        node = by_name.get(model.ref)
        if node is None:
            continue
        table = f"{node['database']}.{node['schema']}.{node.get('alias') or node['name']}"
        registry.models[name] = _replace_table(model, table)


def _replace_table(model: SemanticModel, table: str) -> SemanticModel:
    return SemanticModel(
        name=model.name,
        description=model.description,
        ref=model.ref,
        table=table,
        time_dimension=model.time_dimension,
        pre_aggregated=model.pre_aggregated,
        entities=model.entities,
        dimensions=model.dimensions,
        measures=model.measures,
    )


def validate_registry(
    registry: Registry,
    raw: dict[str, Any] | None = None,
    manifest: dict[str, Any] | None = None,
) -> list[str]:
    """Every problem with this registry, as readable lines. Empty means valid.

    Every problem rather than the first: a PR that fixes one error and hits the
    next on the following run turns a gate into a queue.
    """
    problems: list[str] = []
    raw = raw or {}

    for model in registry.models.values():
        problems += _validate_model(model)

    seen: set[str] = set()
    for entry in raw.get("metrics") or []:
        name = entry.get("name")
        if name in seen:
            problems.append(f"metric {name!r}: defined more than once")
        seen.add(name)

    for metric in registry.metrics.values():
        problems += _validate_metric(registry, metric)

    if manifest is not None:
        problems += _validate_against_manifest(registry, manifest)

    return problems


def _validate_model(model: SemanticModel) -> list[str]:
    problems = []
    where = f"semantic_model {model.name!r}"

    if not model.time_dimension:
        problems.append(f"{where}: no defaults.agg_time_dimension")
    elif model.time_dimension not in model.dimensions:
        problems.append(
            f"{where}: agg_time_dimension {model.time_dimension!r} is not a declared dimension"
        )
    elif not model.dimensions[model.time_dimension].is_time:
        problems.append(f"{where}: agg_time_dimension {model.time_dimension!r} is not type time")

    for measure in model.measures.values():
        if measure.agg not in _AGGS:
            problems.append(
                f"{where}: measure {measure.name!r} has unknown agg {measure.agg!r} "
                f"(one of {', '.join(sorted(_AGGS))})"
            )
        # The hazard a type check cannot catch: `total_spent` is a double and
        # `avg` is a valid agg, but the column is already a SUM.
        if model.pre_aggregated and measure.agg in _AGG_BANNED_ON_PRE_AGGREGATED:
            problems.append(
                f"{where}: measure {measure.name!r} uses agg {measure.agg!r} over a "
                f"pre_aggregated model - {measure.expr!r} is already an aggregate, so "
                f"this is an average of sums. Use sum, or source the metric from the "
                f"underlying model."
            )
    return problems


def _validate_metric(registry: Registry, metric: Metric) -> list[str]:
    problems = []
    where = f"metric {metric.name!r}"

    # The highest-leverage rule in the spec: a metric that does not say what it
    # leaves out cannot be audited, and writing the exclusion is what surfaces
    # that two metrics disagree.
    if not metric.excludes:
        problems.append(f"{where}: meta.excludes is mandatory and missing")
    if not metric.description:
        problems.append(f"{where}: meta.description is mandatory and missing")
    if not metric.owner:
        problems.append(f"{where}: meta.owner is mandatory and missing")
    if metric.grain_min not in _GRAIN_ORDER:
        problems.append(
            f"{where}: grain_min {metric.grain_min!r} is not one of {', '.join(GRAINS)}"
        )

    if metric.type not in ("simple", "ratio"):
        problems.append(f"{where}: type {metric.type!r} is not simple or ratio (v1 scope)")
        return problems

    if metric.type == "ratio" and not (metric.numerator and metric.denominator):
        problems.append(f"{where}: a ratio metric needs both numerator and denominator")
        return problems
    if metric.type == "simple" and not metric.measure:
        problems.append(f"{where}: a simple metric needs type_params.measure")
        return problems

    if not metric.model:
        owners = {
            measure: sorted(
                model.name for model in registry.models.values() if measure in model.measures
            )
            for measure in metric.measures
        }
        missing = [measure for measure, found in owners.items() if not found]
        if missing:
            problems.append(
                f"{where}: measure(s) {', '.join(repr(m) for m in missing)} are not declared "
                f"on any semantic_model"
            )
        else:
            problems.append(
                f"{where}: spans more than one model ({owners}) and no join is declared. "
                f"v1 compiles single-model metrics only."
            )
        return problems

    model = registry.models[metric.model]
    if model.time_dimension and metric.grain_min in _GRAIN_ORDER:
        model_grain = model.dimensions[model.time_dimension].granularity or "day"
        if _GRAIN_ORDER[metric.grain_min] < _GRAIN_ORDER.get(model_grain, 0):
            problems.append(
                f"{where}: grain_min {metric.grain_min!r} is finer than model "
                f"{model.name!r}'s {model_grain!r} time granularity"
            )
    return problems


def _validate_against_manifest(registry: Registry, manifest: dict[str, Any]) -> list[str]:
    """Resolve every ref() and every expr against what dbt actually builds.

    This is the G3 gate. It only works because the models the registry reads
    document all their columns in schema.yml - dbt's manifest carries documented
    columns only, so an undocumented column is indistinguishable from a missing
    one and is reported as an error rather than skipped.
    """
    problems = []
    nodes = {
        node["name"]: node
        for node in (manifest.get("nodes") or {}).values()
        if node.get("resource_type") == "model"
    }

    for model in registry.models.values():
        where = f"semantic_model {model.name!r}"
        node = nodes.get(model.ref)
        if node is None:
            problems.append(
                f"{where}: ref('{model.ref}') matches no dbt model "
                f"(known: {', '.join(sorted(nodes)) or 'none'})"
            )
            continue

        columns = set(node.get("columns") or {})
        if not columns:
            problems.append(
                f"{where}: dbt model {model.ref!r} documents no columns in schema.yml, so no "
                f"expr can be checked against it. Document them - the manifest carries only "
                f"documented columns."
            )
            continue

        for kind, items in (
            ("dimension", model.dimensions.values()),
            ("measure", model.measures.values()),
            ("entity", model.entities),
        ):
            for item in items:
                # Only a bare column reference is checkable. The registry keeps
                # exprs bare by convention; anything else is a SQL fragment the
                # compiler cannot validate, which §3.1 forbids outright.
                if not item.expr.isidentifier():
                    problems.append(
                        f"{where}: {kind} {item.name!r} expr {item.expr!r} is not a bare column "
                        f"reference. The registry may not carry SQL fragments."
                    )
                elif item.expr not in columns:
                    problems.append(
                        f"{where}: {kind} {item.name!r} references column {item.expr!r}, "
                        f"which dbt model {model.ref!r} does not have "
                        f"(has: {', '.join(sorted(columns))})"
                    )
    return problems

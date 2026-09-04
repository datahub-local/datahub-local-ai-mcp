"""Deriving the fleet's shape from the fleet rather than from a config file.

A hardware class is the kernel flavour plus the architecture, which is not an
approximation of the comparability rule but *is* the rule. Node names, classes
and sensor coverage are therefore never written down. See ../README.md.
"""

from __future__ import annotations

import re

# A kernel release is a numeric version followed by a distributor/tree suffix,
# e.g. `6.12.96+deb13-amd64` -> version `6.12.96`, flavour `deb13-amd64`. The
# numbers are what legitimately differs between two machines of the same kind;
# the suffix is what makes them the same kind.
_VERSION_HEAD = re.compile(r"^\d+(?:\.\d+)*")


def kernel_flavour(release: str) -> str:
    """The part of a kernel release that identifies its tree, not its version.

    Returns ``"unversioned"`` when a release carries no numeric head, so an
    unexpected string still groups with its identical twins instead of raising.
    """
    if not release:
        return "unknown"
    stripped = _VERSION_HEAD.sub("", release, count=1).lstrip("+-.")
    return stripped or "unversioned"


def kernel_version(release: str) -> str:
    """The numeric head of a kernel release, i.e. what differs within a flavour."""
    match = _VERSION_HEAD.match(release or "")
    return match.group(0) if match else ""


def version_key(release: str) -> tuple[int, ...]:
    """Sortable form of the numeric head. String comparison gets this backwards
    and would name the wrong node as the one trailing its class."""
    return tuple(int(part) for part in kernel_version(release).split(".") if part.isdigit())


# Position in a kernel version -> (singular, plural) for a difference there.
# Beyond the third position the numbering is a distributor revision, not upstream.
_POSITION = (
    ("major version", "major versions"),
    ("minor series", "minor series"),
    ("patch release", "patch releases"),
    ("revision", "revisions"),
)


def version_gap(behind: str, ahead: str) -> str:
    """How far ``behind`` trails ``ahead``, in words, or "" if it does not.

    The model must never compute this itself. Given two bare version strings a
    4B model invents the difference: asked about `6.12.96` against `6.12.101` it
    answered "6 months behind" and "15 minor versions", where the truth is five
    patch releases inside one series - and no date is knowable from a kernel
    string at all. So the gap is stated here, in the only unit the readings
    support, and the phrase is the whole answer rather than an input to one.
    """
    older, newer = version_key(behind), version_key(ahead)
    if not older or not newer or older >= newer:
        return ""
    width = max(len(older), len(newer))
    older += (0,) * (width - len(older))
    newer += (0,) * (width - len(newer))
    for index, (left, right) in enumerate(zip(older, newer)):
        if left == right:
            continue
        delta = right - left
        singular, plural = _POSITION[min(index, len(_POSITION) - 1)]
        unit = singular if delta == 1 else plural
        shared = ".".join(str(part) for part in older[:index])
        within = f", both on {shared}" if shared else ""
        # "behind" is inside the phrase so the caller cannot land the shared-series
        # clause mid-sentence: "is 5 patch releases, both on 6.12 behind" was the
        # first attempt and reads as a typo.
        return f"{delta} {unit} behind{within}"
    return ""


def class_name(release: str, machine: str) -> str:
    """The comparability group: kernel tree plus architecture."""
    return f"{kernel_flavour(release)}/{machine or 'unknown-arch'}"


def common_prefix(names: list[str]) -> str:
    """Longest shared prefix of ``names``, trimmed to a separator.

    Derived rather than configured, so it is not another copy of the naming
    scheme.
    """
    if len(names) < 2:
        return ""
    first, last = min(names), max(names)
    limit = 0
    while limit < len(first) and limit < len(last) and first[limit] == last[limit]:
        limit += 1
    prefix = first[:limit]
    cut = max(prefix.rfind("-"), prefix.rfind("."), prefix.rfind("_"))
    return prefix[: cut + 1] if cut >= 0 else ""


def shorten(name: str, prefix: str) -> str:
    """Drop ``prefix`` from ``name``, keeping the full name if that would empty it."""
    if prefix and name.startswith(prefix) and len(name) > len(prefix):
        return name[len(prefix) :]
    return name


def group_by_class(kernels: dict[str, str], machines: dict[str, str]) -> dict[str, list[str]]:
    """``{class: [node, ...]}`` for every node with a known kernel."""
    grouped: dict[str, list[str]] = {}
    for node, release in kernels.items():
        grouped.setdefault(class_name(release, machines.get(node, "")), []).append(node)
    return {key: sorted(value) for key, value in sorted(grouped.items())}


def kernel_drift(
    kernels: dict[str, str], machines: dict[str, str]
) -> list[tuple[str, list[str], list[str]]]:
    """Per class, return ``(class, members, nodes_behind)``.

    ``nodes_behind`` is empty unless the class holds more than one distinct
    version, so a class of one is never a finding - it has nothing to compare
    against, and a difference across classes can never be actioned.
    """
    out = []
    for group, members in group_by_class(kernels, machines).items():
        versions = {node: kernels[node] for node in members}
        if len(members) < 2 or len(set(versions.values())) < 2:
            out.append((group, members, []))
            continue
        newest = max(version_key(release) for release in versions.values())
        behind = sorted(
            node for node, release in versions.items() if version_key(release) < newest
        )
        out.append((group, members, behind))
    return out

"""Choose the metadata field that defines Class, or decline to choose one and say so.

`field_based_proposal` has always taken `selected_fields` from its caller. For a person
reanalysing one accession that is right: they read the study, they pick the column. For an
agent reanalysing a repository there was no layer at all, and the only ranking available -
`candidate_fields` - scores every column a submitter happened to write, so Batch and
Analytical order carry positive weight and a column that varies for a technical reason can
outrank one that varies for a biological one.

This module chooses only from columns the submitter **declared** as experimental factors.
A declaration is evidence: someone who deposited `Factor Value[Treatment]` stated that
treatment is what the experiment varied. A `Characteristics[...]` column, a free-text
comment, or the study abstract states no such thing, and guessing a contrast from prose is
how a reanalysis invents a design its data never had. When nothing was declared, this
declines, and the unit is still analysable - MS-DIAL's peak detection, alignment and
annotation do not read Class at all - it simply carries no contrast.

Every outcome is recorded rather than implied: which column was adopted and why, which were
rejected and on what measurement, and what a researcher should check before believing the
grouping. The record travels in the proposal's `warnings`, which the catalog persists.
"""

from __future__ import annotations

import re
from typing import Any

from .class_proposal import (
    _field_contains,
    _field_priority,
    _scalar,
    analysis_samples,
    field_based_proposal,
)
from .models import ClassProposal


# Anchored shapes only. "Dilution factor", "Tissue Factor" (the coagulation protein) and
# "Treatment Factor" all contain the word and none of them declares an experimental factor,
# so a substring test would adopt a measured protein concentration as a study design.
_DECLARED_FACTOR_SHAPES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^factor\s*values?\s*\[\s*(?P<name>.+?)\s*\]$", re.IGNORECASE),
    re.compile(r"^factors?\s*\[\s*(?P<name>.+?)\s*\]$", re.IGNORECASE),
    re.compile(r"^(?P<name>[^\[\]()]+?)\s*\(\s*factors?\s*\)$", re.IGNORECASE),
    re.compile(r"^factors?\s*\d*\s*\(\s*(?P<name>.+?)\s*\)$", re.IGNORECASE),
    re.compile(r"^(?:experimental\s+)?factors?\s*(?P<name>\d*)\**$", re.IGNORECASE),
    re.compile(r"^factors?\s+(?P<name>(?!values?\b)[^\[\]()]+)$", re.IGNORECASE),
)

# Declared, and still never a contrast. These name how the samples were handled, not what the
# experiment varied; a run grouped by Batch reports the laboratory, not the biology.
_NUISANCE_TOKENS: tuple[str, ...] = (
    "replicate", "batch", "plate", "well", "position", "block", "rmsd", "plate effect",
    "injection", "injection number", "run order", "analytical order", "acquisition order",
    "sequence", "order", "analysis date", "run date", "acquisition date", "measurement date",
    "sample number", "sample id", "subject id", "patient id", "animal id",
    "file", "filename", "raw file", "derivatization",
    "spectrum type", "scan polarity", "ion mode", "polarity", "instrument",
    "column", "chromatography", "ionization", "mass analyzer",
    # Found in MetaboLights MTBLS1572, which declares Factor Value[Data acquisition mode] with
    # the levels DDA, DIA and Full-scan. Grouping by it would report the acquisition method as
    # the biology, and the unit itself needs splitting because the campaign accepts one
    # acquisition mode per run.
    "acquisition", "acquisition mode", "data acquisition", "scan mode", "scan type",
    "ms level", "collision", "collision energy", "fragmentation", "mass range", "analytical condition",
)

# Declared, and a contrast only when nothing better was declared. The project contract asks
# that continuous fields not be used merely because they are available; these are the ones
# submitters most often deposit for adjustment rather than for comparison.
_COVARIATE_TOKENS: tuple[str, ...] = (
    "age", "gender", "sex", "bmi", "body mass index", "weight", "height",
    "race", "ethnicity", "birth",
)

# Values that name an absence. MS-DIAL would take "unknown" as a group and compare it against
# the treated animals, so these are counted as missing and reported as missing.
_MISSING_VALUES: frozenset[str] = frozenset(
    {"", "-", "--", "n/a", "na", "n.a.", "nan", "none", "null", "unknown", "not applicable",
     "not available", "not specified", "not collected", "missing", "?"}
)

_SAMPLE_TYPE_TOKENS: tuple[str, ...] = (
    "qc", "qstd", "blank", "standard", "pool", "pooled", "reference",
)

MAX_CROSSED_CLASSES = 12
MIN_COVERAGE = 0.6


def declared_factor_name(field: str) -> str | None:
    """Return the factor's own name when `field` is a declared factor column, else None.

    A bare `Factor` column declares a factor without naming it, which is the Metabolomics
    Workbench shape; it returns "" - declared, unnamed - and not None.
    """
    text = str(field or "").strip()
    for shape in _DECLARED_FACTOR_SHAPES:
        match = shape.match(text)
        if match:
            name = (match.group("name") or "").strip()
            return "" if name.isdigit() else name
    return None


def select_class_fields(unit: dict[str, Any], purpose: str = "") -> dict[str, Any]:
    """Decide which declared factor columns define Class for this unit, or abstain."""
    samples = analysis_samples(unit)
    total = len(samples)
    considered: list[dict[str, Any]] = []
    undeclared = 0
    seen: set[str] = set()

    for field in _fields_of(samples):
        if field in seen:
            continue
        seen.add(field)
        name = declared_factor_name(field)
        if name is None:
            undeclared += 1
            continue
        considered.append(_examine(field, name, samples, total))

    considered.sort(key=_rank)
    eligible = [item for item in considered if item["verdict"] == "eligible"]
    contrasts = [item for item in eligible if item["role"] == "contrast"]
    covariates = [item for item in eligible if item["role"] == "covariate"]

    decision: dict[str, Any] = {
        "unit_id": str(unit.get("unit_id") or ""),
        "purpose": purpose,
        "sample_count": total,
        "declared_factor_columns": len(considered),
        "undeclared_columns": undeclared,
        "considered": considered,
        "selected_fields": [],
        "decision": "abstained",
        "reason": "",
        "rationale": "",
        "notice": "",
        "warnings": [],
        "design_covariates": [
            item["field"] for item in considered if item["role"] == "nuisance"
        ],
    }

    chosen = contrasts or covariates
    if not chosen:
        return _abstain(decision, considered, undeclared)

    primary = chosen[0]
    selected = [primary]
    second = _crossable(primary, chosen[1:], samples)
    if second is not None:
        selected.append(second)

    decision["selected_fields"] = [item["field"] for item in selected]
    decision["decision"] = "declared"
    decision["reason"] = "declared_factor"
    decision["rationale"] = _rationale(selected, total, purpose)
    decision["warnings"] = _warnings(selected, considered, purpose)
    decision["notice"] = (
        "Class was chosen by the catalog from the columns the submitter declared as "
        f"experimental factors ({', '.join(item['field'] for item in selected)}), without a "
        "person reading the study. It does not affect peak detection, alignment or "
        "annotation; check the grouping before reading any comparison from this run."
    )
    return decision


def automatic_class_proposal(
    unit: dict[str, Any], purpose: str
) -> tuple[ClassProposal | None, dict[str, Any]]:
    """Build a Class proposal from the declared factors, or return None with the reason.

    Returning None is a result, not a failure: the unit is analysable without a contrast, and
    the decision record says why none was defined.
    """
    decision = select_class_fields(unit, purpose)
    if decision["decision"] != "declared":
        return None, decision
    proposal = field_based_proposal(
        unit, purpose, decision["selected_fields"], decision["rationale"]
    )
    proposal.model = "catalog-declared-factor-selection"
    proposal.warnings = [decision["notice"], *decision["warnings"]]
    proposal.contrast_definition = {
        "kind": "declared_factor",
        "fields": decision["selected_fields"],
        "levels": _levels(unit, decision["selected_fields"]),
    }
    return proposal, decision


def _fields_of(samples: list[dict[str, Any]]) -> list[str]:
    fields: list[str] = []
    for sample in samples:
        for field in sample.get("attributes", {}) or {}:
            fields.append(str(field))
    return fields


def _examine(
    field: str, name: str, samples: list[dict[str, Any]], total: int
) -> dict[str, Any]:
    raw = [_scalar((sample.get("attributes") or {}).get(field)) for sample in samples]
    values = [_value(item) for item in raw]
    present = [value for value in values if value]
    placeholders = sorted({(item or "(empty)") for item, kept in zip(raw, values) if not kept})
    levels: dict[str, int] = {}
    for value in present:
        levels[value] = levels.get(value, 0) + 1
    distinct = len(levels)
    singletons = sum(1 for count in levels.values() if count == 1)
    coverage = (len(present) / total) if total else 0.0
    role = _role(name or field)

    if distinct < 2:
        verdict = "no_contrast"
        reason = (
            "every sample carries the same value, so it defines no comparison"
            if distinct == 1
            else "no sample carries a value"
        )
    elif coverage < MIN_COVERAGE:
        verdict = "too_sparse"
        reason = (
            f"only {len(present)} of {total} samples carry a value, "
            "so most samples would be grouped as NA"
        )
    elif total >= 4 and distinct == len(present):
        verdict = "identifier"
        reason = "every sample has its own value, so it identifies rather than groups"
    elif distinct > max(2, total // 2):
        verdict = "too_granular"
        reason = f"{distinct} values across {total} samples leaves groups too small to compare"
    elif distinct > MAX_CROSSED_CLASSES and singletons * 2 > distinct:
        # A germplasm panel and a continuous score both produce many levels; what separates
        # them is replication. Factor Value[Cultivar] gives 129 levels of five samples each and
        # is a design; Factor Value[Frailty Index Score] gives 702 levels, most of them one
        # sample, and is a measurement the contract says not to group by.
        verdict = "unreplicated"
        reason = (
            f"{singletons} of {distinct} values occur in a single sample, so the column measures "
            "each sample rather than grouping them"
        )
    elif role == "nuisance":
        verdict = "nuisance"
        reason = "names how the samples were handled, not what the experiment varied"
    else:
        verdict = "eligible"
        reason = "declared as an experimental factor and groups the samples"

    return {
        "field": field,
        "factor_name": name,
        "role": role,
        "verdict": verdict,
        "reason": reason,
        "present": len(present),
        "total": total,
        "distinct_count": distinct,
        "singleton_levels": singletons,
        "coverage": round(coverage, 3),
        "placeholder_values": placeholders,
        "levels": dict(sorted(levels.items(), key=lambda item: (-item[1], item[0]))[:12]),
        "smallest_group": min(levels.values()) if levels else 0,
        "sample_type_levels": sorted(
            value for value in levels if _looks_like_sample_type(value)
        ),
    }


def _role(name: str) -> str:
    text = name.casefold()
    if any(_field_contains(text, token) for token in _NUISANCE_TOKENS):
        return "nuisance"
    if any(_field_contains(text, token) for token in _COVARIATE_TOKENS):
        return "covariate"
    return "contrast"


def _rank(item: dict[str, Any]) -> tuple:
    """Deterministic preference among the declared factors, most defensible first.

    Shape outranks vocabulary. A column that groups the samples two to twelve ways with no
    missing values is the one a reader would call the design, whatever the submitter named it;
    the existing field vocabulary only breaks ties between columns of equally good shape.
    """
    workable = 0 if 2 <= item["distinct_count"] <= MAX_CROSSED_CLASSES else 1
    return (
        0 if item["verdict"] == "eligible" else 1,
        {"contrast": 0, "covariate": 1, "nuisance": 2}[item["role"]],
        0 if item["coverage"] >= 0.999 else 1,
        workable,
        -_field_priority(item["factor_name"] or item["field"]),
        -item["smallest_group"],
        item["field"].casefold(),
    )


def _crossable(
    primary: dict[str, Any], rest: list[dict[str, Any]], samples: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Add a second factor only when the crossing is a design the data actually supports.

    Two declared factors are not automatically a two-factor design: crossing them can produce
    as many groups as there are samples, which is the identifier case reached by another route.
    The crossing is adopted only when every crossed cell holds at least two samples, which is
    what a factorial design looks like and what an incidental pairing does not.
    """
    for candidate in rest:
        if candidate["role"] != "contrast":
            continue
        cells: dict[tuple[str, str], int] = {}
        for sample in samples:
            attributes = sample.get("attributes") or {}
            key = (
                _value(attributes.get(primary["field"])) or "NA",
                _value(attributes.get(candidate["field"])) or "NA",
            )
            cells[key] = cells.get(key, 0) + 1
        if len(cells) <= primary["distinct_count"] or len(cells) > MAX_CROSSED_CLASSES:
            continue
        if min(cells.values()) < 2:
            continue
        return candidate
    return None


def _rationale(selected: list[dict[str, Any]], total: int, purpose: str) -> str:
    named = ", ".join(f"{item['field']} ({item['distinct_count']} levels)" for item in selected)
    head = (
        f"Class from the declared experimental factor {named} across {total} samples."
        if len(selected) == 1
        else f"Class from the crossed declared experimental factors {named} across {total} samples."
    )
    if any(item["role"] == "covariate" for item in selected):
        head += (
            " No declared factor other than a subject covariate was usable, so this groups by a"
            " covariate rather than by a designed contrast."
        )
    if purpose.strip():
        head += f" Stated purpose: {purpose.strip()}"
    return head


def _warnings(
    selected: list[dict[str, Any]], considered: list[dict[str, Any]], purpose: str
) -> list[str]:
    warnings: list[str] = []
    for item in selected:
        if item["coverage"] < 0.999:
            # The submitter's own placeholder is kept as the Class label rather than rewritten,
            # so the warning names it: "unknown" becomes a Class called unknown, an empty cell
            # becomes NA, and in neither case is the sample merged into a biological group.
            warnings.append(
                f"{item['field']} carries no value for {item['total'] - item['present']} of "
                f"{item['total']} samples ({', '.join(item['placeholder_values'])}); those "
                "samples become their own Class and are not merged into a biological group."
            )
        if item["smallest_group"] < 3:
            warnings.append(
                f"{item['field']} has a group of {item['smallest_group']} sample(s); "
                "a comparison at that size is descriptive, not statistical."
            )
        if item["sample_type_levels"]:
            warnings.append(
                f"{item['field']} mixes sample-type values "
                f"({', '.join(item['sample_type_levels'])}) with biological values; "
                "they become their own Class rather than joining a biological group."
            )
        if item["role"] == "covariate":
            warnings.append(
                f"{item['field']} is a subject covariate, not a designed contrast; it was used "
                "because no other declared factor was usable."
            )
    dropped = [item for item in considered if item["verdict"] != "eligible"]
    if dropped:
        warnings.append(
            "Declared factors not used: "
            + "; ".join(f"{item['field']} ({item['verdict']})" for item in dropped[:8])
            + ("; ..." if len(dropped) > 8 else "")
        )
    if not purpose.strip():
        warnings.append(
            "No analysis purpose was stated, so the grouping was chosen from the declared "
            "factors alone and not against a question."
        )
    return warnings


def _abstain(
    decision: dict[str, Any], considered: list[dict[str, Any]], undeclared: int
) -> dict[str, Any]:
    if not considered:
        decision["reason"] = "no_declared_factor"
        decision["rationale"] = (
            f"No experimental factor was declared for this unit; {undeclared} other metadata "
            "columns were present and none of them states what the experiment varied."
        )
        decision["notice"] = (
            "No Class was proposed: the submitter declared no experimental factor. The unit is "
            "analysable without one, and the run carries no contrast. A contrast must come from "
            "a person reading the study, not from the catalog guessing at the other columns."
        )
    else:
        decision["reason"] = "no_usable_declared_factor"
        decision["rationale"] = (
            "Every declared experimental factor was unusable: "
            + "; ".join(
                f"{item['field']} ({item['verdict']}: {item['reason']})"
                for item in considered[:8]
            )
            + ("; ..." if len(considered) > 8 else "")
        )
        decision["notice"] = (
            "No Class was proposed: factors were declared but none of them groups these samples "
            "into a comparison. The measurements behind that judgement are in the decision "
            "record, so a person can overrule it."
        )
    decision["warnings"] = [decision["notice"]]
    return decision


def _levels(unit: dict[str, Any], fields: list[str]) -> dict[str, list[str]]:
    samples = analysis_samples(unit)
    levels: dict[str, list[str]] = {}
    for field in fields:
        values = {
            _value((sample.get("attributes") or {}).get(field)) or "NA" for sample in samples
        }
        levels[field] = sorted(values)
    return levels


def _value(value: Any) -> str:
    text = _scalar(value)
    return "" if text.casefold() in _MISSING_VALUES else text


def _looks_like_sample_type(value: str) -> bool:
    text = value.casefold()
    return any(_field_contains(text, token) for token in _SAMPLE_TYPE_TOKENS)

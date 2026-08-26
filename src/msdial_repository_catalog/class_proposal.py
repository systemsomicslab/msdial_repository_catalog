from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from typing import Any

from .models import ClassAssignment, ClassProposal, stable_id


CLASS_PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["purpose", "selected_fields", "rationale", "assignments"],
    "properties": {
        "purpose": {"type": "string"},
        "selected_fields": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "rationale": {"type": "string", "minLength": 1},
        "contrast_definition": {"type": "object"},
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["sample_id", "class_label", "values"],
                "properties": {
                    "sample_id": {"type": "string"},
                    "class_label": {"type": "string"},
                    "values": {"type": "object"},
                },
            },
        },
    },
}


def build_class_proposal_request(unit: dict[str, Any], purpose: str) -> dict[str, Any]:
    fields = candidate_fields(unit)
    return {
        "task": "Propose MS-DIAL Class labels and an explicit statistical contrast.",
        "purpose": purpose,
        "analysis_unit": {
            key: unit.get(key)
            for key in (
                "unit_id", "repository", "accession", "title", "label", "separation",
                "chromatography", "ion_mode", "acquisition_mode", "target_omics",
            )
        },
        "candidate_fields": fields,
        "samples": [
            {
                "sample_id": sample["sample_id"],
                "raw_file": sample.get("raw_file", ""),
                "attributes": sample.get("attributes", {}),
                "contexts": sample.get("contexts", []),
            }
            for sample in unit.get("samples", [])
        ],
        "instructions": [
            "Use biological fields that answer the stated purpose; do not choose fields only because they vary.",
            "Keep Blank, QC, Standard, batch, pairing, and analytical order available as design covariates.",
            "Do not merge unknown values with a biological group.",
            "Join selected values with underscore; normalize underscores inside each value to hyphens.",
            "Return one assignment for every sample and cite the source fields in rationale.",
            "Treat study-level topics as hypotheses, not sample-level assignments.",
        ],
        "output_schema": CLASS_PROPOSAL_SCHEMA,
    }


def candidate_fields(unit: dict[str, Any]) -> list[dict[str, Any]]:
    samples = unit.get("samples", [])
    total = len(samples)
    values: dict[str, list[str]] = defaultdict(list)
    for sample in samples:
        for field, value in sample.get("attributes", {}).items():
            text = _scalar(value)
            if text:
                values[str(field)].append(text)
    result = []
    for field, items in values.items():
        distinct = sorted(set(items), key=str.casefold)
        score = _field_priority(field) + (len(items) / max(1, total)) * 2
        if len(distinct) == 1:
            score -= 5
        elif len(distinct) <= max(20, total // 2):
            score += 2
        result.append(
            {
                "field": field,
                "present": len(items),
                "total": total,
                "distinct_count": len(distinct),
                "examples": distinct[:8],
                "priority_score": round(score, 3),
            }
        )
    return sorted(result, key=lambda item: (-item["priority_score"], item["field"].casefold()))


def field_based_proposal(
    unit: dict[str, Any],
    purpose: str,
    selected_fields: list[str],
    rationale: str = "Deterministic field-based proposal for review.",
) -> ClassProposal:
    if not selected_fields:
        raise ValueError("At least one metadata field is required.")
    assignments = []
    for sample in unit.get("samples", []):
        attributes = sample.get("attributes", {})
        values = {field: _scalar(attributes.get(field)) or "NA" for field in selected_fields}
        label = "_".join(_class_token(values[field]) or "NA" for field in selected_fields)
        assignments.append(
            ClassAssignment(sample_id=str(sample["sample_id"]), class_label=label, values=values)
        )
    payload = json.dumps(
        {"unit_id": unit["unit_id"], "purpose": purpose, "fields": selected_fields, "assignments": [a.class_label for a in assignments]},
        ensure_ascii=False,
        sort_keys=True,
    )
    proposal = ClassProposal(
        proposal_id=stable_id("class-proposal", payload),
        unit_id=str(unit["unit_id"]),
        purpose=purpose,
        selected_fields=selected_fields,
        assignments=assignments,
        rationale=rationale,
        model="deterministic-field-projection",
        prompt_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    )
    validate_class_proposal(unit, proposal)
    return proposal


def validate_class_proposal(unit: dict[str, Any], proposal: ClassProposal) -> None:
    expected = [str(item["sample_id"]) for item in unit.get("samples", [])]
    actual = [item.sample_id for item in proposal.assignments]
    duplicates = [sample for sample, count in Counter(actual).items() if count > 1]
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    problems = []
    if duplicates:
        problems.append(f"duplicate samples: {', '.join(duplicates)}")
    if missing:
        problems.append(f"missing samples: {', '.join(missing)}")
    if extra:
        problems.append(f"unknown samples: {', '.join(extra)}")
    if not proposal.rationale.strip():
        problems.append("rationale is empty")
    if not proposal.selected_fields:
        problems.append("selected_fields is empty")
    for assignment in proposal.assignments:
        if not assignment.class_label.strip():
            problems.append(f"empty Class for {assignment.sample_id}")
        if "__" in assignment.class_label or assignment.class_label.startswith("_"):
            problems.append(f"invalid Class delimiter for {assignment.sample_id}")
    if problems:
        raise ValueError("Invalid Class proposal: " + "; ".join(problems))


def _field_priority(field: str) -> float:
    text = field.casefold()
    if any(
        token in text
        for token in (
            "analyticalcondition", "analytical condition", "instrument", "chromatograph",
            "ionization", "collision", "fragmentation", "mass range", "scan mode",
        )
    ):
        return -8.0
    priorities = (
        ("disease", 6), ("treatment", 6), ("genotype", 6), ("condition", 5),
        ("cell type", 5), ("tissue", 5), ("organ", 5), ("age", 5),
        ("sex", 4), ("diet", 4), ("time", 3), ("batch", 2),
        ("analytical order", 1), ("instrument", -3), ("file", -4),
    )
    return float(next((value for token, value in priorities if _field_contains(text, token)), 0))


def _field_contains(text: str, token: str) -> bool:
    escaped = re.escape(token).replace(r"\ ", r"\s+")
    return bool(re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", text))


def _class_token(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).strip().replace("_", "-")
    text = re.sub(r"\s+", "-", text)
    text = "".join(character if character.isalnum() or character in ".+-" else "-" for character in text)
    return re.sub(r"-+", "-", text).strip("-")


def _scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(filter(None, (_scalar(item) for item in value)))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return re.sub(r"\s+", " ", str(value)).strip()

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


def build_class_proposal_request(
    unit: dict[str, Any],
    purpose: str,
    *,
    include_samples: bool = False,
    sample_limit: int = 0,
) -> dict[str, Any]:
    samples = analysis_samples(unit)
    analysis_unit = {**unit, "samples": samples}
    fields = candidate_fields(analysis_unit)
    request = {
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
        "sample_count": len(samples),
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
    if include_samples:
        if sample_limit > 0:
            samples = samples[:sample_limit]
        request["samples"] = [
            {
                "sample_id": sample["sample_id"],
                "raw_file": sample.get("raw_file", ""),
                "attributes": sample.get("attributes", {}),
                "contexts": sample.get("contexts", []),
            }
            for sample in samples
        ]
    return request


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
    for sample in analysis_samples(unit):
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
    expected = [str(item["sample_id"]) for item in analysis_samples(unit)]
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


def analysis_samples(unit: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one metadata row per primary MS-DIAL analysis input."""
    samples = [dict(item) for item in unit.get("samples", [])]
    files = normalize_file_roles(unit.get("files", []))
    known_paths = {
        str(item.get("path") or item.get("name") or "").replace("\\", "/").casefold()
        for item in files
    }
    primary = [item for item in files if _file_role(item, known_paths) == "raw"]
    if not primary:
        return samples

    selected: list[dict[str, Any]] = []
    used_rows: set[int] = set()
    for raw in primary:
        raw_path = str(raw.get("path") or raw.get("name") or "")
        raw_sample = str(raw.get("sample_id") or "")
        match_index = next(
            (
                index
                for index, sample in enumerate(samples)
                if index not in used_rows
                and _same_raw_file(str(sample.get("raw_file") or ""), raw_path)
            ),
            None,
        )
        if match_index is None and raw_sample:
            match_index = next(
                (
                    index
                    for index, sample in enumerate(samples)
                    if index not in used_rows
                    and str(sample.get("sample_id") or "").casefold() == raw_sample.casefold()
                ),
                None,
            )
        if match_index is None:
            sample = {
                "sample_id": raw_sample or _primary_stem(raw_path),
                "raw_file": raw_path,
                "attributes": {},
                "contexts": [],
            }
        else:
            used_rows.add(match_index)
            sample = dict(samples[match_index])
            sample["raw_file"] = raw_path
        sample["related_files"] = [
            str(item.get("path") or item.get("name") or "")
            for item in files
            if _primary_stem(str(item.get("path") or item.get("name") or ""))
            == _primary_stem(raw_path)
            and _file_role(item, known_paths) != "raw"
        ]
        selected.append(sample)
    return selected


def normalize_file_roles(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Classify primary, alternate, sidecar, and auxiliary raw-data containers."""
    result = [dict(item) for item in files]
    known_paths = {
        str(item.get("path") or item.get("name") or "").replace("\\", "/").casefold()
        for item in result
    }
    for item in result:
        item["role"] = _file_role(item, known_paths)
    # A sidecar or auxiliary container arrives carrying a sample_id derived from
    # its own basename, which names no row in the sample table: half a manifest's
    # rows held a dangling key, and anything joining files to samples on it either
    # dropped those rows or re-invented the samples. Point each one at the
    # analytical sample it belongs to, or say plainly that it belongs to none.
    primary_by_stem: dict[str, dict[str, Any]] = {}
    for item in result:
        if item["role"] == "raw":
            primary_by_stem.setdefault(
                _primary_stem(str(item.get("path") or item.get("name") or "")), item
            )
    for item in result:
        if item["role"] == "raw":
            item["sample_id_resolved"] = True
            continue
        parent = primary_by_stem.get(
            _primary_stem(str(item.get("path") or item.get("name") or ""))
        )
        if parent is None:
            item["sample_id"] = ""
            item["sample_id_resolved"] = False
            continue
        item["sample_id"] = str(parent.get("sample_id") or "")
        item["parent_file"] = str(parent.get("path") or parent.get("name") or "")
        item["sample_id_resolved"] = bool(item["sample_id"])
    return result


def normalize_analysis_unit(unit: dict[str, Any]) -> dict[str, Any]:
    """Return the canonical analysis-unit view used by every Catalog surface."""
    files = normalize_file_roles(unit.get("files", []))
    normalized = {**unit, "files": files}
    normalized["samples"] = analysis_samples(normalized)
    normalized["sample_count"] = len(normalized["samples"])
    normalized["analysis_file_count"] = sum(
        str(item.get("role") or "raw") == "raw" for item in files
    )
    return normalized


def _file_role(item: dict[str, Any], known_paths: set[str]) -> str:
    path = str(item.get("path") or item.get("name") or "").replace("\\", "/").casefold()
    if path.endswith(".timeseries.data"):
        return "auxiliary"
    if path.endswith(".wiff.scan"):
        return "sidecar"
    role = str(item.get("role") or "raw")
    if path.endswith(".wiff2") and path.removesuffix(".wiff2") + ".wiff" in known_paths:
        return "raw_alternate"
    return role


def _primary_stem(path: str) -> str:
    value = path.replace("\\", "/").casefold()
    for suffix in (".timeseries.data", ".wiff.scan", ".wiff2", ".wiff"):
        if value.endswith(suffix):
            return value[: -len(suffix)]
    return value


def _same_raw_file(left: str, right: str) -> bool:
    left_value = left.replace("\\", "/").casefold()
    right_value = right.replace("\\", "/").casefold()
    return (
        left_value == right_value
        or left_value.rsplit("/", 1)[-1] == right_value.rsplit("/", 1)[-1]
    )


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

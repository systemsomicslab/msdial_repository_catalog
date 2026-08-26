from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .context import extract_contexts, extract_study_topics
from .models import (
    AnalysisUnit,
    Evidence,
    RawFileRecord,
    SampleRecord,
    StudyRecord,
    stable_id,
)


def project_to_study(project: dict[str, Any], parser_version: str = "legacy-json-v1") -> StudyRecord:
    repository = str(project.get("repository") or "unknown").strip()
    accession = str(project.get("accession") or "unknown").strip()
    study = StudyRecord(
        repository=repository,
        accession=accession,
        title=str(project.get("title") or ""),
        description=str(project.get("description") or ""),
        public_url=str(project.get("public_url") or ""),
        license=str(project.get("license") or ""),
        publications=list(project.get("publications") or []),
        source_payload=project,
        source_urls=list(project.get("metadata_sources") or []),
        retrieved_at=datetime.now(timezone.utc).isoformat(),
        source_updated_at=str(project.get("source_updated_at") or ""),
        parser_version=parser_version,
    )
    declared_units = project.get("analysis_units")
    if isinstance(declared_units, list) and declared_units:
        study.analysis_units = [
            _unit_from_payload(study, item, index)
            for index, item in enumerate(declared_units, start=1)
        ]
    else:
        study.analysis_units = [_legacy_project_unit(study, project)]
    return study


def _legacy_project_unit(study: StudyRecord, project: dict[str, Any]) -> AnalysisUnit:
    source_id = str(project.get("assay_name") or project.get("analysis_id") or "project")
    payload = {
        "source_subrecord_id": source_id,
        "label": source_id,
        "separation": project.get("separation"),
        "chromatography": project.get("chromatography"),
        "ion_mode": project.get("ion_mode"),
        "acquisition_mode": project.get("acquisition_mode"),
        "ion_mobility": project.get("ion_mobility"),
        "instrument": project.get("instrument"),
        "target_omics": project.get("target_omics") or _infer_omics(study.title, study.description),
        "untargeted": project.get("untargeted"),
        "review_status": project.get("selection_status") or "unreviewed",
        "sample_metadata": project.get("sample_metadata") or [],
        "files": project.get("files") or [],
        "evidence": project.get("evidence") or [],
        "warnings": [
            *list(project.get("warnings") or []),
            "Legacy project metadata represented one accession as one analysis unit; review mixed methods.",
        ],
    }
    return _unit_from_payload(study, payload, 1)


def _unit_from_payload(study: StudyRecord, payload: dict[str, Any], index: int) -> AnalysisUnit:
    source_id = str(payload.get("source_subrecord_id") or payload.get("analysis_id") or f"unit-{index}")
    unit = AnalysisUnit(
        unit_id=stable_id(study.repository, study.accession, source_id, _signature_payload(payload)),
        source_subrecord_id=source_id,
        label=str(payload.get("label") or source_id),
        separation=_value(payload, "separation"),
        chromatography=_value(payload, "chromatography"),
        ion_mode=_value(payload, "ion_mode"),
        acquisition_mode=_value(payload, "acquisition_mode"),
        ion_mobility=_value(payload, "ion_mobility"),
        instrument=str(payload.get("instrument") or ""),
        target_omics=str(payload.get("target_omics") or _infer_omics(study.title, study.description)),
        untargeted=_optional_bool(payload.get("untargeted")),
        review_status=str(payload.get("review_status") or "unreviewed"),
        warnings=list(payload.get("warnings") or []),
    )
    unit.samples = [_sample_from_payload(item, index) for index, item in enumerate(payload.get("sample_metadata") or [], start=1)]
    study_text = f"{study.title} {study.description}"
    unit.contexts = extract_study_topics(study_text, study.public_url or study.study_id)
    for sample in unit.samples:
        sample.contexts = extract_contexts(sample, study.public_url or study.study_id)
    unit.files = [_file_from_payload(item) for item in payload.get("files") or []]
    unit.evidence = [_evidence_from_payload(item, study.public_url or study.study_id) for item in payload.get("evidence") or []]
    _enrich_unit_from_consistent_sample_metadata(unit, study.public_url or study.study_id)
    return unit


def _sample_from_payload(payload: dict[str, Any], index: int) -> SampleRecord:
    return SampleRecord(
        sample_id=str(payload.get("sample_id") or f"sample-{index}"),
        source_name=str(payload.get("source_name") or ""),
        raw_file=str(payload.get("raw_file") or ""),
        attributes=dict(payload.get("values") or payload.get("attributes") or {}),
    )


def _file_from_payload(payload: dict[str, Any]) -> RawFileRecord:
    return RawFileRecord(
        path=str(payload.get("name") or payload.get("path") or ""),
        role=str(payload.get("role") or "raw"),
        size_bytes=int(payload.get("size_bytes") or 0),
        checksum=str(payload.get("checksum") or ""),
        download_url=str(payload.get("url") or payload.get("download_url") or ""),
        sample_id=str(payload.get("sample_id") or ""),
    )


def _evidence_from_payload(value: Any, source: str) -> Evidence:
    if isinstance(value, dict):
        return Evidence(
            source=str(value.get("source") or source),
            source_field=str(value.get("source_field") or ""),
            source_value=str(value.get("source_value") or ""),
            method=str(value.get("method") or "declared"),
            confidence=float(value.get("confidence") or 1.0),
            note=str(value.get("note") or ""),
        )
    return Evidence(source=source, note=str(value))


def _value(payload: dict[str, Any], key: str) -> str:
    return str(payload.get(key) or "Unknown")


def _optional_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "untargeted"}


def _signature_payload(payload: dict[str, Any]) -> str:
    return "|".join(
        str(payload.get(key) or "Unknown")
        for key in (
            "separation", "chromatography", "ion_mode", "acquisition_mode",
            "ion_mobility", "instrument", "target_omics",
        )
    )


def _infer_omics(*values: str) -> str:
    text = " ".join(values).casefold()
    if re.search(r"\blipid(?:ome|omic|omics)?\b", text):
        return "Lipidomics"
    if re.search(r"\bmetabol(?:ome|omic|omics)?\b", text):
        return "Metabolomics"
    return "Unknown"


def _enrich_unit_from_consistent_sample_metadata(unit: AnalysisUnit, source: str) -> None:
    rules = (
        ("chromatography", ("chromatography type",), _normalize_chromatography),
        ("instrument", ("instrument",), lambda value: value),
        ("ion_mobility", ("ion mobility",), _normalize_ion_mobility),
        ("separation", ("method type",), _normalize_separation),
        ("acquisition_mode", ("instrument mode", "acquisition mode"), _normalize_acquisition),
        ("ion_mode", ("ion polarity", "ion mode"), _normalize_ion_mode),
    )
    for property_name, field_tokens, normalizer in rules:
        current = str(getattr(unit, property_name) or "")
        if current and current != "Unknown":
            continue
        match = _consistent_attribute(unit.samples, field_tokens)
        if match is None:
            continue
        field_name, source_value = match
        normalized = normalizer(source_value)
        if not normalized or normalized == "Unknown":
            continue
        setattr(unit, property_name, normalized)
        unit.evidence.append(
            Evidence(
                source=source,
                source_field=field_name,
                source_value=source_value,
                method="consistent-sample-metadata",
                confidence=0.95,
                note=f"All {len(unit.samples)} sample records reported the same value.",
            )
        )


def _consistent_attribute(
    samples: list[SampleRecord], field_tokens: tuple[str, ...]
) -> tuple[str, str] | None:
    if not samples:
        return None
    matches = []
    for sample in samples:
        candidates = [
            (str(field), str(value).strip())
            for field, value in sample.attributes.items()
            if str(value).strip()
            and any(token in str(field).casefold() for token in field_tokens)
        ]
        candidate = next(
            (
                item for item in candidates
                if item[0].rsplit("/", 1)[-1].strip().casefold() in field_tokens
            ),
            candidates[0] if candidates else None,
        )
        if candidate is None:
            return None
        matches.append(candidate)
    values = {value.casefold() for _, value in matches}
    return matches[0] if len(values) == 1 else None


def _normalize_chromatography(value: str) -> str:
    text = value.casefold()
    if "reversed" in text or re.search(r"\brp\b", text):
        return "Reversed phase"
    if "hilic" in text or "hydrophilic interaction" in text:
        return "HILIC"
    if "normal phase" in text:
        return "Normal phase"
    return value.strip()


def _normalize_ion_mobility(value: str) -> str:
    text = value.strip().casefold()
    if text in {"yes", "true", "on", "enabled", "1"}:
        return "Enabled"
    if text in {"no", "false", "off", "disabled", "0"}:
        return "Disabled"
    return value.strip()


def _normalize_separation(value: str) -> str:
    text = value.casefold()
    if "gc" in text:
        return "GC-MS"
    if "lc" in text:
        return "LC-MS"
    if "direct" in text or "infusion" in text or "flow injection" in text:
        return "DI-MS"
    return "Unknown"


def _normalize_acquisition(value: str) -> str:
    text = value.casefold()
    if "dda" in text or "data dependent" in text:
        return "DDA"
    if "swath" in text or "dia" in text or "data independent" in text:
        return "DIA"
    if "aif" in text or "all ion" in text:
        return "AIF"
    return "Unknown"


def _normalize_ion_mode(value: str) -> str:
    text = value.casefold()
    if "negative" in text or text == "neg":
        return "Negative"
    if "positive" in text or text == "pos":
        return "Positive"
    if "switch" in text or "both" in text:
        return "Both"
    return "Unknown"

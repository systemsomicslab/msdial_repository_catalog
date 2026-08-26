from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any


def stable_id(*parts: object, length: int = 20) -> str:
    payload = "\x1f".join(str(part or "").strip() for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


@dataclass(slots=True)
class Evidence:
    source: str
    source_field: str = ""
    source_value: str = ""
    method: str = "declared"
    confidence: float = 1.0
    note: str = ""


@dataclass(slots=True)
class ContextAssertion:
    category: str
    value: str
    normalized_value: str = ""
    ontology_id: str = ""
    evidence: Evidence | None = None


@dataclass(slots=True)
class SampleRecord:
    sample_id: str
    source_name: str = ""
    raw_file: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    contexts: list[ContextAssertion] = field(default_factory=list)


@dataclass(slots=True)
class RawFileRecord:
    path: str
    role: str = "raw"
    size_bytes: int = 0
    checksum: str = ""
    download_url: str = ""
    sample_id: str = ""


@dataclass(slots=True)
class AnalysisUnit:
    unit_id: str
    source_subrecord_id: str
    label: str = ""
    separation: str = "Unknown"
    chromatography: str = "Unknown"
    ion_mode: str = "Unknown"
    acquisition_mode: str = "Unknown"
    ion_mobility: str = "Unknown"
    instrument: str = ""
    target_omics: str = "Unknown"
    untargeted: bool | None = None
    review_status: str = "unreviewed"
    samples: list[SampleRecord] = field(default_factory=list)
    files: list[RawFileRecord] = field(default_factory=list)
    contexts: list[ContextAssertion] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def signature(self) -> str:
        return "|".join(
            (
                self.separation,
                self.chromatography,
                self.ion_mode,
                self.acquisition_mode,
                self.ion_mobility,
                self.instrument,
                self.target_omics,
            )
        )


@dataclass(slots=True)
class StudyRecord:
    repository: str
    accession: str
    title: str = ""
    description: str = ""
    public_url: str = ""
    license: str = ""
    publications: list[dict[str, str]] = field(default_factory=list)
    analysis_units: list[AnalysisUnit] = field(default_factory=list)
    source_payload: dict[str, Any] = field(default_factory=dict)
    source_urls: list[str] = field(default_factory=list)
    retrieved_at: str = ""
    source_updated_at: str = ""
    parser_version: str = ""

    @property
    def study_id(self) -> str:
        return f"{self.repository}:{self.accession}"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def source_hash(self) -> str:
        value = json.dumps(self.source_payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class ClassAssignment:
    sample_id: str
    class_label: str
    values: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ClassProposal:
    proposal_id: str
    unit_id: str
    purpose: str
    selected_fields: list[str]
    assignments: list[ClassAssignment]
    rationale: str
    contrast_definition: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    prompt_hash: str = ""
    status: str = "proposed"
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class AnalysisRun:
    run_id: str
    unit_id: str
    msdial_version: str
    parameter_hash: str
    mztab_path: str = ""
    mztab_sha256: str = ""
    qa_status: str = "not_evaluated"


@dataclass(slots=True)
class ContrastDefinition:
    contrast_id: str
    run_id: str
    name: str
    case_definition: dict[str, Any]
    control_definition: dict[str, Any]
    covariates: list[str] = field(default_factory=list)
    pairing_field: str = ""


@dataclass(slots=True)
class MetaboliteResponse:
    response_id: str
    contrast_id: str
    observation_id: str
    direction: str
    log2_fold_change: float | None = None
    effect_size: float | None = None
    p_value: float | None = None
    adjusted_p_value: float | None = None
    statistic_method: str = ""
    evidence_level: str = "observational"

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Any

from .models import ContextAssertion, Evidence, SampleRecord


FIELD_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("species", ("species", "organism", "taxon")),
    ("organ", ("organ", "tissue", "anatomical", "sample source", "biosource")),
    ("cell_type", ("cell type", "cell line", "cell subtype", "cell_type")),
    ("disease", ("disease", "diagnosis", "pathology", "health status", "phenotype")),
    ("inflammation", ("inflammation", "inflammatory", "cytokine", "immune activation")),
    ("aging", ("age", "aging", "age group", "developmental stage", "senescence")),
    ("sex", ("sex", "gender")),
    ("genotype", ("genotype", "strain", "gene", "knockout", "mutation")),
    ("intervention", ("treatment", "drug", "diet", "intervention", "exposure", "perturbation")),
    ("time", ("time", "time point", "duration", "sampling time")),
    ("condition", ("condition", "group", "class", "cohort")),
    ("sample_role", ("sample type", "file type", "quality control", "blank")),
)

CONTEXT_VOCABULARY: dict[str, tuple[str, ...]] = {
    "aging": ("aging", "aged", "elderly", "old", "senescence", "senescent"),
    "inflammation": ("inflammation", "inflammatory", "cytokine", "immune activation"),
    "stress": ("stress", "oxidative stress", "starvation", "hypoxia"),
    "development": ("development", "developmental", "differentiation", "maturation"),
    "recovery": ("recovery", "regeneration", "repair", "remission"),
    "nutrition": ("diet", "nutrition", "feeding", "fasting", "starvation"),
    "disease": ("disease", "cancer", "diabetes", "infection", "degeneration"),
}


def normalize_field_name(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def normalize_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(filter(None, (normalize_value(item) for item in value)))
    if isinstance(value, dict):
        return "; ".join(f"{key}={normalize_value(item)}" for key, item in sorted(value.items()))
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value))).strip()


def context_category(field_name: str) -> str:
    normalized = normalize_field_name(field_name)
    if any(
        token in normalized
        for token in (
            "analyticalcondition", "analytical condition", "chromatography",
            "instrument", "ionization", "collision energy", "fragmentation",
            "acquisition", "mass spectrometry", "scan mode", "mass range",
        )
    ):
        return ""
    for category, names in FIELD_RULES:
        if any(name == normalized or _contains_token(normalized, name) for name in names):
            return category
    return ""


def extract_contexts(
    sample: SampleRecord,
    source: str,
) -> list[ContextAssertion]:
    contexts: list[ContextAssertion] = []
    seen: set[tuple[str, str, str]] = set()
    for field_name, raw_value in sample.attributes.items():
        value = normalize_value(raw_value)
        if not value:
            continue
        category = context_category(field_name)
        if category:
            _append_context(contexts, seen, category, value, source, field_name, value, "field-map", 0.95)
        lowered = value.casefold()
        for vocabulary, tokens in CONTEXT_VOCABULARY.items():
            if any(_contains_token(lowered, token) for token in tokens):
                _append_context(
                    contexts, seen, "biological_process", vocabulary, source,
                    field_name, value, "controlled-vocabulary", 0.8,
                )
    return contexts


def extract_study_topics(study_text: str, source: str) -> list[ContextAssertion]:
    contexts: list[ContextAssertion] = []
    seen: set[tuple[str, str, str]] = set()
    lowered = normalize_value(study_text).casefold()
    for vocabulary, tokens in CONTEXT_VOCABULARY.items():
        if any(_contains_token(lowered, token) for token in tokens):
            _append_context(
                contexts, seen, "study_topic", vocabulary, source,
                "title/description", study_text, "study-text", 0.6,
                "Study-level topic; do not treat as a sample assignment without review.",
            )
    return contexts


def context_values(samples: Iterable[SampleRecord]) -> str:
    return " ".join(
        assertion.normalized_value or assertion.value
        for sample in samples
        for assertion in sample.contexts
    )


def _append_context(
    target: list[ContextAssertion],
    seen: set[tuple[str, str, str]],
    category: str,
    value: str,
    source: str,
    field_name: str,
    source_value: str,
    method: str,
    confidence: float,
    note: str = "",
) -> None:
    normalized = normalize_value(value)
    key = (category, normalized.casefold(), field_name.casefold())
    if key in seen:
        return
    seen.add(key)
    target.append(
        ContextAssertion(
            category=category,
            value=value,
            normalized_value=normalized,
            evidence=Evidence(
                source=source,
                source_field=field_name,
                source_value=source_value,
                method=method,
                confidence=confidence,
                note=note,
            ),
        )
    )


def _contains_token(text: str, token: str) -> bool:
    escaped = re.escape(token.casefold()).replace(r"\ ", r"\s+")
    return bool(re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", text))

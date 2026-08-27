from __future__ import annotations

import csv
import urllib.parse
from pathlib import Path
from typing import Any

from .common import (
    RepositoryHttpClient,
    assay_rows,
    display_field_name,
    extract_publications,
    first_matching_value,
    infer_acquisition,
    infer_chromatography,
    infer_ion_mobility,
    infer_ion_mode,
    infer_omics,
    infer_separation,
    infer_untargeted,
    metadata_scalar,
    parse_apache_index,
    source_evidence,
)


class MetaboLightsAdapter:
    name = "metabolights"
    api = "https://www.ebi.ac.uk/metabolights/ws"
    public = "https://ftp.ebi.ac.uk/pub/databases/metabolights/studies/public"

    def __init__(self, client: RepositoryHttpClient | None = None) -> None:
        self.client = client or RepositoryHttpClient()

    def list_accessions(self) -> list[str]:
        payload = self.client.get_json(f"{self.api}/studies/technology")
        return sorted(
            {
                str(item["accession"])
                for item in payload
                if infer_separation(item.get("technology")) in {"GC-MS", "LC-MS"}
            }
        )

    def inspect_metadata(self, accession: str) -> dict[str, Any]:
        accession = accession.upper()
        study_url = f"{self.api}/studies/{accession}"
        study_payload = self.client.get_json(study_url)
        investigation = study_payload.get("isaInvestigation", {})
        mtbls = study_payload.get("mtblsStudy", {})
        studies = investigation.get("studies") or []
        study = studies[0] if studies else {}
        listing_url = f"{self.api}/studies/{accession}/assays"
        listing = self.client.get_json(listing_url)
        assay_names = [
            str(item.get("filename") or "")
            for item in listing.get("data", {}).get("assays", [])
            if item.get("filename")
        ]
        files_root = f"{self.public}/{accession}/FILES/"
        try:
            file_index = parse_apache_index(self.client.get_text(files_root))
        except Exception:
            file_index = {}
        study_table_url = f"{self.public}/{accession}/s_{accession}.txt"
        try:
            study_table = self.client.get_text(study_table_url)
        except Exception:
            study_table = ""
        material_values = _material_values(study)
        for key, values in _study_table_values(study_table).items():
            material_values.setdefault(key, {}).update(values)
        units = []
        assay_payloads: dict[str, Any] = {}
        for index, assay_name in enumerate(assay_names, start=1):
            assay_url = f"{self.api}/studies/{accession}/{urllib.parse.quote(assay_name)}"
            payload = self.client.get_json(assay_url)
            assay_payloads[assay_name] = payload
            rows = assay_rows(payload)
            samples = _samples(rows, material_values)
            files = _files(accession, rows, file_index, self.public)
            text_values = [assay_name, *[metadata_scalar(value) for row in rows for value in row.values()]]
            combined = " ".join(text_values)
            separation = infer_separation(assay_name, combined)
            ion_mode = infer_ion_mode(assay_name, combined)
            acquisition = infer_acquisition(combined)
            warnings = []
            if not files:
                warnings.append("This assay did not expose raw or derived spectral data references.")
            if files and any(int(item.get("size_bytes") or 0) <= 0 for item in files):
                warnings.append("One or more spectral file sizes were unavailable from the public index.")
            if acquisition == "Unknown" and separation == "LC-MS":
                warnings.append("DDA/DIA/AIF must be confirmed from raw scan headers before reanalysis.")
            units.append(
                {
                    "source_subrecord_id": assay_name,
                    "label": assay_name,
                    "separation": separation,
                    "chromatography": infer_chromatography(combined),
                    "ion_mode": ion_mode,
                    "acquisition_mode": acquisition,
                    "ion_mobility": infer_ion_mobility(combined),
                    "instrument": _consistent_row_value(rows, ("Parameter Value[Instrument]", "Instrument")),
                    "target_omics": infer_omics(study.get("title"), study.get("description"), combined),
                    "untargeted": infer_untargeted(study.get("title"), study.get("description"), combined),
                    "review_status": "needs_review" if (separation == "LC-MS" and acquisition == "Unknown") or ion_mode == "Unknown" else "unreviewed",
                    "sample_metadata": samples,
                    "files": files,
                    "evidence": [
                        source_evidence(assay_url, "assay filename", assay_name),
                        source_evidence(assay_url, "Scan polarity", ion_mode),
                        source_evidence(assay_url, "Instrument", _consistent_row_value(rows, ("Parameter Value[Instrument]", "Instrument"))),
                    ],
                    "warnings": warnings,
                }
            )
        return {
            "repository": self.name,
            "accession": accession,
            "title": str(study.get("title") or investigation.get("title") or ""),
            "description": str(study.get("description") or ""),
            "public_url": f"https://www.ebi.ac.uk/metabolights/{accession}",
            "license": str(mtbls.get("datasetLicense") or _comment_value(study, "License")),
            "publications": extract_publications(study.get("publications", [])),
            "metadata_sources": [study_url, listing_url, study_table_url, files_root],
            "source_updated_at": str(mtbls.get("updatedDate") or mtbls.get("releaseDate") or ""),
            "analysis_units": units,
            "repository_metadata": {
                "study_api": study_payload,
                "assay_listing": listing,
                "assays": assay_payloads,
                "study_table": study_table,
            },
        }


def _files(
    accession: str, rows: list[dict[str, Any]], index: dict[str, int], root: str
) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for row in rows:
        for field in ("Raw Spectral Data File", "Derived Spectral Data File", "Raw Data File"):
            value = metadata_scalar(row.get(field))
            if not value:
                continue
            normalized = value.replace("\\", "/").lstrip("/")
            if not normalized.upper().startswith("FILES/"):
                normalized = "FILES/" + normalized
            relative = normalized[6:]
            lower = relative.casefold()
            role = "converted" if lower.endswith((".mzml", ".mzxml", ".mzdata.xml")) else "raw"
            selected.setdefault(
                lower,
                {
                    "name": value,
                    "size_bytes": index.get(relative, index.get(Path(relative).name, 0)),
                    "url": f"{root}/{accession}/" + urllib.parse.quote(normalized, safe="/"),
                    "role": role,
                },
            )
    return sorted(selected.values(), key=lambda item: item["name"].casefold())


def _samples(rows: list[dict[str, Any]], materials: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    result = []
    for index, row in enumerate(rows, start=1):
        raw_file = first_matching_value(row, ("raw spectral data file", "derived spectral data file", "raw data file"))
        sample_id = first_matching_value(row, ("sample name", "source name", "sample identifier")) or Path(raw_file.replace("\\", "/")).stem or f"sample_{index}"
        source_name = first_matching_value(row, ("source name",)) or sample_id
        values = dict(materials.get(sample_id.casefold(), {}))
        values.update(materials.get(source_name.casefold(), {}))
        for key, value in row.items():
            if str(key).strip().casefold() in {
                "raw spectral data file", "derived spectral data file", "raw data file", "sample name", "source name"
            }:
                continue
            text = metadata_scalar(value)
            if text:
                values[display_field_name(key)] = text
        result.append({"sample_id": sample_id, "source_name": source_name, "raw_file": raw_file, "values": values})
    return result


def _material_values(study: dict[str, Any]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for group_name in ("sources", "samples"):
        for item in study.get("materials", {}).get(group_name, []) or []:
            if not isinstance(item, dict):
                continue
            name = metadata_scalar(item.get("name"))
            if not name:
                continue
            values = result.setdefault(name.casefold(), {})
            for collection in ("characteristics", "factorValues"):
                for entry in item.get(collection, []) or []:
                    if not isinstance(entry, dict):
                        continue
                    field = metadata_scalar(entry.get("category") or entry.get("factorName") or entry.get("name"))
                    value = metadata_scalar(entry.get("value") or entry.get("annotationValue"))
                    if field and value:
                        values[field] = value
    return result


def _study_table_values(text: str) -> dict[str, dict[str, str]]:
    lines = [line for line in text.splitlines() if line.strip()]
    result: dict[str, dict[str, str]] = {}
    if not lines:
        return result
    for row in csv.DictReader(lines, delimiter="\t"):
        sample = first_matching_value(row, ("sample name",))
        if not sample:
            continue
        values = result.setdefault(sample.casefold(), {})
        for key, value in row.items():
            text_value = metadata_scalar(value)
            if text_value and ("characteristics[" in key.casefold() or "factor value[" in key.casefold()):
                values[display_field_name(key)] = text_value
    return result


def _consistent_row_value(rows: list[dict[str, Any]], names: tuple[str, ...]) -> str:
    values = {
        first_matching_value(row, names)
        for row in rows
        if first_matching_value(row, names)
    }
    return next(iter(values)) if len(values) == 1 else ""


def _comment_value(study: dict[str, Any], name: str) -> str:
    for item in study.get("comments", []) or []:
        if str(item.get("name") or "").casefold() == name.casefold():
            return str(item.get("value") or "")
    return ""

from __future__ import annotations

import csv
import hashlib
import json
import urllib.parse
from pathlib import Path
from typing import Any

from .common import (
    RepositoryHttpClient,
    display_field_name,
    extract_publications,
    infer_acquisition,
    infer_chromatography,
    infer_ion_mobility,
    infer_ion_mode,
    infer_omics,
    infer_separation,
    infer_untargeted,
    metadata_scalar,
    parse_sdrf,
    sidecar_name,
    source_evidence,
    technical_signature,
)


class MetaboBankAdapter:
    name = "metabobank"
    search_api = "https://ddbj.nig.ac.jp/search/api"

    def __init__(self, client: RepositoryHttpClient | None = None) -> None:
        self.client = client or RepositoryHttpClient()

    def list_accessions(self) -> list[str]:
        result = []
        page = 1
        while True:
            payload = self.client.get_json(f"{self.search_api}/entries/metabobank/?page={page}&perPage=100")
            result.extend(str(item.get("identifier") or "") for item in payload.get("items", []) if item.get("identifier"))
            if not payload.get("pagination", {}).get("hasNext"):
                break
            page += 1
        return sorted(set(result))

    def inspect_metadata(self, accession: str) -> dict[str, Any]:
        accession = accession.upper()
        metadata_url = f"{self.search_api}/entries/metabobank/{accession}"
        entry = self.client.get_json(metadata_url)
        data_root = _data_root(entry)
        if not data_root:
            raise ValueError("MetaboBank did not publish a DATA distribution URL.")
        filelist_url = urllib.parse.urljoin(data_root, f"{accession}.filelist.txt")
        sdrf_url = urllib.parse.urljoin(data_root, f"{accession}.sdrf.txt")
        idf_url = urllib.parse.urljoin(data_root, f"{accession}.idf.txt")
        file_rows = _filelist(self.client.get_text(filelist_url))
        sdrf_text = self.client.get_text(sdrf_url)
        rows = parse_sdrf(sdrf_text)
        study_text = " ".join(
            [
                metadata_scalar(entry.get("title")) or _property(entry, "Study Title"),
                metadata_scalar(entry.get("description")) or _property(entry, "Study Description"),
                _property(entry, "Comment[Study type]"),
                _property(entry, "Comment[Experiment type]"),
                _property(entry, "Comment[Submission type]"),
                json.dumps(entry.get("properties") or {}, ensure_ascii=False),
            ]
        )
        grouped: dict[tuple[str, ...], dict[str, Any]] = {}
        for row in rows:
            fields = _technical_fields(row, study_text)
            signature = technical_signature(fields)
            group = grouped.setdefault(signature, {"fields": fields, "rows": []})
            group["rows"].append(row)
        if not grouped:
            fields = _technical_fields({}, study_text)
            grouped[technical_signature(fields)] = {"fields": fields, "rows": []}
        units = []
        for index, group in enumerate(grouped.values(), start=1):
            fields = group["fields"]
            references = _raw_references(group["rows"])
            files, fallback = _files(file_rows, references, data_root)
            warnings = []
            if fallback:
                warnings.append("Original vendor raw references were unavailable; ABF files were selected as a fallback.")
            if not files:
                warnings.append("SDRF raw-data references did not match downloadable file-list entries.")
            if fields["ion_mode"] in {"Unknown", "Both"} and fields["separation"] == "LC-MS":
                warnings.append("Polarity must be confirmed or split using raw scan headers.")
            if fields["acquisition_mode"] == "Unknown" and fields["separation"] == "LC-MS":
                warnings.append("DDA/DIA/AIF must be confirmed from raw scan headers.")
            signature_text = "|".join(technical_signature(fields))
            source_id = f"sdrf-{index}-{hashlib.sha256(signature_text.encode()).hexdigest()[:8]}"
            units.append(
                {
                    "source_subrecord_id": source_id,
                    "label": _label(fields),
                    **fields,
                    "untargeted": infer_untargeted(study_text, *[" ".join(row.values()) for row in group["rows"]]),
                    "review_status": "needs_review" if warnings else "unreviewed",
                    "sample_metadata": _samples(group["rows"]),
                    "files": files,
                    "evidence": [
                        source_evidence(sdrf_url, "technical signature", _label(fields)),
                        source_evidence(sdrf_url, "SDRF rows", len(group["rows"])),
                    ],
                    "warnings": warnings,
                }
            )
        title = metadata_scalar(entry.get("title")) or _property(entry, "Study Title")
        description = metadata_scalar(entry.get("description")) or _property(entry, "Study Description")
        return {
            "repository": self.name,
            "accession": accession,
            "title": title,
            "description": description,
            "public_url": str(entry.get("url") or f"https://ddbj.nig.ac.jp/search/entry/metabobank/{accession}"),
            "license": metadata_scalar(entry.get("license")),
            "publications": _publications(entry),
            "metadata_sources": [metadata_url, idf_url, sdrf_url, filelist_url],
            "source_updated_at": metadata_scalar(entry.get("dateModified")) or _property(entry, "Comment[Last Update Date]"),
            "analysis_units": units,
            "repository_metadata": {"search_entry": entry, "data_root": data_root, "sdrf_rows": rows, "filelist": file_rows},
        }


def _property(entry: dict[str, Any], name: str) -> str:
    return metadata_scalar((entry.get("properties") or {}).get(name))


def _data_root(entry: dict[str, Any]) -> str:
    for item in entry.get("distribution", []) or []:
        if str(item.get("encodingFormat") or "").upper() == "DATA":
            return str(item.get("contentUrl") or "").rstrip("/") + "/"
    return ""


def _filelist(text: str) -> list[dict[str, Any]]:
    result = []
    for row in csv.DictReader(text.splitlines(), delimiter="\t"):
        name = str(row.get("Name") or "").replace("\\", "/").lstrip("/")
        if name:
            result.append(
                {
                    "type": str(row.get("Type") or ""),
                    "name": name,
                    "size": int(row.get("Size") or 0),
                    "md5": str(row.get("MD5") or "").strip(),
                }
            )
    return result


def _technical_fields(row: dict[str, str], study_text: str) -> dict[str, str]:
    technical = " ".join(
        value
        for key, value in row.items()
        if value and any(token in key.casefold() for token in (
            "instrument", "chromat", "column", "scan polarity", "ion mode", "acquisition", "mass analyzer", "protocol"
        ))
    )
    combined = f"{study_text} {technical}"
    instrument = next(
        (value for key, value in row.items() if value and "instrument" in key.casefold()),
        "",
    )
    return {
        "separation": infer_separation(combined),
        "chromatography": infer_chromatography(combined),
        "ion_mode": infer_ion_mode(_row_values(row, "scan polarity", "ion mode", "polarity"), technical),
        "acquisition_mode": infer_acquisition(_row_values(row, "acquisition", "instrument mode", "scan type"), combined),
        "ion_mobility": infer_ion_mobility(combined),
        "instrument": instrument,
        "target_omics": infer_omics(study_text),
    }


def _row_values(row: dict[str, str], *tokens: str) -> str:
    return " ".join(value for key, value in row.items() if value and any(token in key.casefold() for token in tokens))


def _raw_references(rows: list[dict[str, str]]) -> list[str]:
    result = []
    for row in rows:
        for key, value in row.items():
            if key.startswith("Raw Data File") and value:
                normalized = value.replace("\\", "/").lstrip("/")
                if normalized not in result:
                    result.append(normalized)
    originals = [item for item in result if not item.casefold().endswith(".abf") and "/abf/" not in item.casefold()]
    return originals or result


def _files(
    file_rows: list[dict[str, Any]], references: list[str], data_root: str
) -> tuple[list[dict[str, Any]], bool]:
    selected: dict[str, dict[str, Any]] = {}
    fallback = bool(references) and all(item.casefold().endswith(".abf") or "/abf/" in item.casefold() for item in references)
    for reference in references:
        prefix = reference.rstrip("/")
        lower = prefix.casefold()
        for row in file_rows:
            name = str(row.get("name") or "")
            candidate = name.casefold()
            if candidate == lower or candidate.startswith(lower + "/") or (lower.endswith((".wiff", ".wiff2")) and candidate.startswith(lower + ".")):
                selected.setdefault(candidate, row)
    result = [
        {
            "name": row["name"],
            "size_bytes": int(row.get("size") or 0),
            "url": urllib.parse.urljoin(data_root, urllib.parse.quote(str(row["name"]), safe="/")),
            "role": "sidecar" if sidecar_name(str(row["name"])) else "raw",
            "checksum": str(row.get("md5") or ""),
        }
        for row in selected.values()
    ]
    return sorted(result, key=lambda item: item["name"].casefold()), fallback


def _samples(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    result = []
    for index, row in enumerate(rows, start=1):
        raw_values = [value for key, value in row.items() if key.startswith("Raw Data File") and value]
        original = next((value for value in raw_values if not value.casefold().endswith(".abf") and "/abf/" not in value.casefold()), raw_values[0] if raw_values else "")
        sample_id = str(row.get("Sample Name") or row.get("Assay Name") or f"sample_{index}")
        values = {display_field_name(key): value for key, value in row.items() if value and key not in {"Source Name", "Sample Name"} and not key.startswith("Raw Data File")}
        result.append(
            {
                "sample_id": sample_id,
                "source_name": str(row.get("Source Name") or sample_id),
                "raw_file": original,
                "values": values,
            }
        )
    return result


def _publications(entry: dict[str, Any]) -> list[dict[str, str]]:
    result = []
    for item in entry.get("publication", []) or []:
        identifier = str(item.get("id") or "")
        kind = str(item.get("dbType") or "").casefold()
        result.append(
            {
                "title": str(item.get("title") or ""),
                "doi": identifier if kind == "doi" else "",
                "pubmed_id": identifier if kind in {"pubmed", "pmid"} else "",
                "url": str(item.get("url") or ""),
            }
        )
    return result or extract_publications(entry.get("properties", {}))


def _label(fields: dict[str, str]) -> str:
    return " / ".join(fields[key] for key in ("separation", "chromatography", "ion_mode", "acquisition_mode"))

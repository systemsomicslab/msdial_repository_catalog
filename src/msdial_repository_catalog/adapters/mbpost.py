from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
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
    sidecar_name,
    source_evidence,
    technical_signature,
)


class MbPostAdapter:
    name = "mb_post"
    base = "https://repository.massbank.jp"

    def __init__(self, client: RepositoryHttpClient | None = None) -> None:
        self.client = client or RepositoryHttpClient()

    def list_accessions(self) -> list[str]:
        payload = self.client.get_json(f"{self.base}/api/projects?limit=1000&offset=0")
        return sorted(str(item["mbpostId"]) for item in payload.get("list", []))

    def inspect_metadata(self, accession: str) -> dict[str, Any]:
        accession = accession.upper()
        project_url = f"{self.base}/api/projects/{accession}"
        project = self.client.get_json(project_url)
        location = str(project.get("location") or f"{accession}.0")
        listing_url = f"{self.base}/api/projects/{location}/files?limit=10000&offset=0"
        listing = self.client.get_json(listing_url)
        raw_items = [item for item in listing.get("list", []) if item.get("type") == "raw"]
        primary = [item for item in raw_items if not sidecar_name(str(item.get("name") or ""))]

        def read_detail(item: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
            url = f"{self.base}/api/projects/{location}/files/{item['id']}"
            try:
                return str(item.get("name") or ""), self.client.get_json(url), ""
            except Exception as error:
                return str(item.get("name") or ""), {}, str(error)

        details: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=min(6, max(1, len(primary)))) as executor:
            for name, detail, error in executor.map(read_detail, primary):
                details[name.casefold()] = detail
                if error:
                    errors[name] = error
        grouped: dict[tuple[str, ...], dict[str, Any]] = {}
        for item in primary:
            name = str(item.get("name") or "")
            detail = details.get(name.casefold(), {})
            fields = _technical_fields(detail, project)
            signature = technical_signature(fields)
            group = grouped.setdefault(
                signature,
                {"fields": fields, "primary": [], "sample_metadata": [], "details": {}},
            )
            group["primary"].append(item)
            group["sample_metadata"].append(_sample(item, detail, project))
            group["details"][name] = detail
        units = []
        for index, group in enumerate(grouped.values(), start=1):
            fields = group["fields"]
            primary_names = {str(item.get("name") or "").casefold() for item in group["primary"]}
            selected_items = list(group["primary"])
            selected_items.extend(
                item for item in raw_items
                if sidecar_name(str(item.get("name") or ""))
                and any(str(item.get("name") or "").casefold().startswith(name) for name in primary_names)
            )
            files = [
                {
                    "name": str(item.get("name") or ""),
                    "size_bytes": int(item.get("size") or 0),
                    "url": f"{self.base}/api/download/{location}",
                    "role": "sidecar" if sidecar_name(str(item.get("name") or "")) else "raw",
                    "checksum": str(item.get("checksum") or ""),
                    "sample_id": Path(str(item.get("name") or "").replace("\\", "/")).stem,
                }
                for item in selected_items
            ]
            preset_names = sorted(
                {
                    _preset_value(detail, "analyticalCondition", "presetName")
                    for detail in group["details"].values()
                    if _preset_value(detail, "analyticalCondition", "presetName")
                }
            )
            warnings = [f"Metadata detail was unavailable for {name}: {error}" for name, error in errors.items() if name.casefold() in primary_names]
            if fields["acquisition_mode"] == "Unknown":
                warnings.append("Acquisition mode requires raw-header confirmation.")
            unit_id = preset_names[0] if len(preset_names) == 1 else f"technical-group-{index}"
            units.append(
                {
                    "source_subrecord_id": unit_id,
                    "label": preset_names[0] if preset_names else _label(fields),
                    **fields,
                    "untargeted": infer_untargeted(project.get("title"), project.get("keywords"), json.dumps(group["details"], ensure_ascii=False)),
                    "review_status": "needs_review" if fields["acquisition_mode"] == "Unknown" else "unreviewed",
                    "sample_metadata": group["sample_metadata"],
                    "files": files,
                    "evidence": [
                        source_evidence(project_url, "analyticalCondition/presetName", "; ".join(preset_names)),
                        source_evidence(project_url, "technical signature", _label(fields)),
                    ],
                    "warnings": warnings,
                }
            )
        return {
            "repository": self.name,
            "accession": accession,
            "title": str(project.get("title") or ""),
            "description": str(project.get("description") or ""),
            "public_url": f"{self.base}/#/projects/{accession}",
            "license": "CC0 1.0",
            "publications": extract_publications(
                {"pubmedId": project.get("pubmedId", ""), "doi": project.get("doi", "")}
            ),
            "metadata_sources": [project_url, listing_url],
            "source_updated_at": str(project.get("modifiedAt") or ""),
            "analysis_units": units,
            "repository_metadata": {"project": project, "file_listing": listing, "raw_file_details": details},
        }


def _technical_fields(detail: dict[str, Any], project: dict[str, Any]) -> dict[str, str]:
    analytical = _preset_group(detail, "analyticalCondition")
    text = json.dumps(analytical, ensure_ascii=False)
    ion_mode = infer_ion_mode(analytical.get("polarity"))
    if ion_mode == "Unknown":
        ion_mode = infer_ion_mode(text)
    return {
        "separation": infer_separation(analytical.get("methodType"), text),
        "chromatography": infer_chromatography(analytical.get("chromatographyType"), text),
        "ion_mode": ion_mode,
        "acquisition_mode": infer_acquisition(analytical.get("instrumentMode"), text),
        "ion_mobility": _ion_mobility(analytical.get("ionMobility"), text),
        "instrument": analytical.get("instrument", ""),
        "target_omics": infer_omics(project.get("title"), project.get("keywords"), text),
    }


def _preset_group(detail: dict[str, Any], category: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for group in detail.get("presets", []) or []:
        if str(group.get("category") or "").casefold() != category.casefold():
            continue
        for item in group.get("presets", []) or []:
            result[str(item.get("key") or item.get("label") or "")] = metadata_scalar(item.get("value"))
    return result


def _preset_value(detail: dict[str, Any], category: str, key: str) -> str:
    return _preset_group(detail, category).get(key, "")


def _sample(item: dict[str, Any], detail: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    raw_file = str(item.get("name") or "")
    sample_id = Path(raw_file.replace("\\", "/")).stem
    values: dict[str, str] = {}
    for key in ("keywords", "principalInvestigator", "affiliation"):
        value = metadata_scalar(project.get(key))
        if value:
            values[display_field_name(key)] = value
    for group in detail.get("presets", []) or []:
        category = display_field_name(group.get("category") or "metadata")
        for preset in group.get("presets", []) or []:
            value = metadata_scalar(preset.get("value"))
            if value:
                label = display_field_name(preset.get("label") or preset.get("key"))
                values[f"{category} / {label}"] = value
    return {"sample_id": sample_id, "source_name": sample_id, "raw_file": raw_file, "values": values}


def _ion_mobility(value: str | None, text: str) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized in {"yes", "true", "on", "enabled", "1"}:
        return "Enabled"
    if normalized in {"no", "false", "off", "disabled", "0"}:
        return "Disabled"
    return infer_ion_mobility(text)


def _label(fields: dict[str, str]) -> str:
    return " / ".join(
        fields[key] for key in ("separation", "chromatography", "ion_mode", "acquisition_mode")
    )

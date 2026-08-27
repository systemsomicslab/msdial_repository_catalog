from __future__ import annotations

import html
import re
import urllib.parse
from typing import Any

from .common import (
    RepositoryHttpClient,
    display_field_name,
    extract_publications,
    html_text,
    infer_acquisition,
    infer_chromatography,
    infer_ion_mobility,
    infer_ion_mode,
    infer_omics,
    infer_separation,
    infer_untargeted,
    parse_name_value_pairs,
    parse_records,
    parse_size,
    parse_tab_blocks,
    source_evidence,
)


class MetabolomicsWorkbenchAdapter:
    name = "metabolomics_workbench"
    base = "https://www.metabolomicsworkbench.org"

    def __init__(self, client: RepositoryHttpClient | None = None) -> None:
        self.client = client or RepositoryHttpClient()

    def list_accessions(self) -> list[str]:
        text = self.client.get_text(f"{self.base}/rest/study/study_id/ST/available/json")
        return sorted(set(re.findall(r"(?m)^study_id\t(ST\d+)", text)))

    def inspect_metadata(self, accession: str) -> dict[str, Any]:
        accession = accession.upper()
        summary_url = f"{self.base}/rest/study/study_id/{accession}/summary/json"
        analysis_url = f"{self.base}/rest/study/study_id/{accession}/analysis/json"
        factors_url = f"{self.base}/rest/study/study_id/{accession}/factors/json"
        summary = parse_tab_blocks(self.client.get_text(summary_url))
        analyses = parse_records(self.client.get_text(analysis_url))
        try:
            factors = parse_records(self.client.get_text(factors_url))
        except Exception:
            factors = []
        row = summary[0] if summary else {}
        public_url = row.get(
            "study_url",
            f"{self.base}/data/DRCCMetadata.php?Mode=Study&StudyID={accession}",
        )
        try:
            detail_text = html_text(self.client.get_text(public_url))
        except Exception:
            detail_text = ""
        download_url = (
            f"{self.base}/data/DRCCStudySummary.php?Mode=SetupRawDataDownload&StudyID={accession}"
        )
        try:
            archives = _download_files(self.client.get_text(download_url), self.base)
        except Exception:
            archives = []
        samples = _samples(factors)
        study_text = " ".join(
            [row.get("study_title", ""), row.get("study_summary", ""), detail_text]
        )
        if not analyses:
            analyses = [{"analysis_id": "study", "analysis_type": row.get("analysis_type", "")}]
        multiple = len(analyses) > 1
        units = []
        for index, analysis in enumerate(analyses, start=1):
            analysis_id = str(analysis.get("analysis_id") or f"analysis-{index}")
            combined = " ".join([study_text, *map(str, analysis.values())])
            ion_mode = infer_ion_mode(analysis.get("ion_mode"), analysis.get("polarity"))
            if ion_mode == "Unknown":
                ion_mode = infer_ion_mode(analysis.get("analysis_summary"), combined)
            unit_files, ambiguous_files = _partition_archives(archives, analysis_id, ion_mode, multiple)
            warnings: list[str] = []
            if multiple:
                warnings.append(
                    "Metabolomics Workbench factors are study-level; verify that each sample belongs to this analysis_id."
                )
            if ambiguous_files:
                warnings.append(
                    "Raw-data archives could not be assigned uniquely to this analysis_id; shared archives are retained."
                )
            if not unit_files:
                warnings.append("No public raw-data archive was identified for this analysis unit.")
            units.append(
                {
                    "source_subrecord_id": analysis_id,
                    "label": str(analysis.get("analysis_type") or analysis_id),
                    "separation": infer_separation(analysis.get("analysis_type"), combined),
                    "chromatography": str(analysis.get("chromatography_type") or infer_chromatography(combined)),
                    "ion_mode": ion_mode,
                    "acquisition_mode": infer_acquisition(combined),
                    "ion_mobility": infer_ion_mobility(combined),
                    "instrument": str(analysis.get("ms_instrument_name") or analysis.get("instrument_name") or analysis.get("instrument") or ""),
                    "target_omics": infer_omics(study_text, combined),
                    "untargeted": infer_untargeted(study_text, combined),
                    "review_status": "needs_review" if multiple or ambiguous_files else "unreviewed",
                    "sample_metadata": samples,
                    "files": unit_files,
                    "evidence": [
                        source_evidence(analysis_url, "analysis_id", analysis_id),
                        source_evidence(analysis_url, "analysis_type", analysis.get("analysis_type", "")),
                        source_evidence(analysis_url, "ion_mode", analysis.get("ion_mode", "")),
                    ],
                    "warnings": warnings,
                }
            )
        return {
            "repository": self.name,
            "accession": accession,
            "title": row.get("study_title", ""),
            "description": row.get("study_summary", ""),
            "public_url": public_url,
            "license": row.get("license", ""),
            "publications": extract_publications(row, detail_text),
            "metadata_sources": [summary_url, analysis_url, factors_url, public_url, download_url],
            "source_updated_at": row.get("last_update", ""),
            "analysis_units": units,
            "repository_metadata": {"summary": summary, "analysis": analyses, "factors": factors},
        }


def _download_files(text: str, base: str) -> list[dict[str, Any]]:
    pattern = re.compile(
        r'<a\s+href=["\'](?P<href>[^"\']+)["\'][^>]*>(?P<name>[^<]+)</a>\s*'
        r'<b>\((?P<size>[^)]+)\)</b>\s*\(Checksum:(?P<checksum>[a-fA-F0-9]+)\)',
        re.IGNORECASE,
    )
    return [
        {
            "name": html.unescape(match.group("name")),
            "size_bytes": parse_size(match.group("size")),
            "url": urllib.parse.urljoin(base, match.group("href")),
            "checksum": match.group("checksum").lower(),
            "role": "raw_archive",
        }
        for match in pattern.finditer(text)
    ]


def _partition_archives(
    archives: list[dict[str, Any]], analysis_id: str, ion_mode: str, multiple: bool
) -> tuple[list[dict[str, Any]], bool]:
    if not multiple:
        return list(archives), False
    tokens = [analysis_id.casefold()]
    if ion_mode == "Positive":
        tokens.extend(("positive", "_pos", "-pos"))
    elif ion_mode == "Negative":
        tokens.extend(("negative", "_neg", "-neg"))
    matched = [item for item in archives if any(token in item["name"].casefold() for token in tokens)]
    if matched:
        return matched, False
    return [{**item, "role": "shared_raw_archive"} for item in archives], bool(archives)


def _samples(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for index, row in enumerate(rows, start=1):
        sample_id = str(
            row.get("local_sample_id") or row.get("sample_id") or row.get("sample") or f"sample_{index}"
        ).strip()
        values: dict[str, str] = {}
        values.update(parse_name_value_pairs(str(row.get("factors") or "")))
        values.update(parse_name_value_pairs(str(row.get("additional_sample_data") or "")))
        for key, value in row.items():
            if key not in {"study_id", "local_sample_id", "sample_id", "sample", "factors"}:
                values.setdefault(display_field_name(key), str(value or "").strip())
        result.append(
            {
                "sample_id": sample_id,
                "source_name": str(row.get("subject_id") or row.get("subject") or sample_id),
                "raw_file": str(row.get("raw_data") or row.get("raw_file") or row.get("filename") or sample_id),
                "values": values,
            }
        )
    return result

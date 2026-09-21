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
            unit_samples, unpartitioned_reason = _partition_samples(samples, ion_mode, multiple)
            warnings: list[str] = []
            if multiple and unpartitioned_reason:
                # The warning this used to carry unconditionally said "verify that each sample
                # belongs to this analysis_id" and nothing ever did. It is now raised only when
                # the split genuinely could not be made, and it says what stopped it.
                warnings.append(
                    "Metabolomics Workbench factors are study-level and this unit's samples could "
                    f"not be split from the study's: {unpartitioned_reason}. Every sample of the "
                    "study is listed here, so verify that each one belongs to this analysis_id "
                    "before running it."
                )
            elif multiple:
                warnings.append(
                    f"Samples were split from the study-level factor table by the ion mode their "
                    f"raw-file names state: {len(unit_samples)} of {len(samples)} belong to this "
                    "analysis unit."
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
                    # needs_review used to mean only "this study had more than one unit", which was true of
                    # every multi-unit study and therefore said nothing. It now means the sample
                    # split could not be made, or the archives could not be assigned to one unit.
                    "review_status": "needs_review" if unpartitioned_reason or ambiguous_files else "unreviewed",
                    "sample_metadata": unit_samples,
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


_POLARITY_TOKEN = re.compile(r"^(?P<mode>pos|neg)(?:itive|ative)?\d*$", re.IGNORECASE)


def file_polarity(name: str) -> str:
    """The ion mode a raw-file name states, or "" when it states none.

    Token-wise rather than by substring, because a substring test reads "pos" out of words that
    have nothing to do with polarity. One real study in this catalog is named
    211210_SVC_Pozzi__Lipidomics_NEG_S01.mzXML, and a submitter surname is not an ion mode.
    """
    tokens = re.split(r"[^A-Za-z0-9]+", str(name or ""))
    found = {
        match.group("mode").casefold()
        for token in tokens
        if (match := _POLARITY_TOKEN.match(token))
    }
    if len(found) != 1:
        return ""
    return "Positive" if found.pop() == "pos" else "Negative"


def _partition_samples(
    samples: list[dict[str, Any]], ion_mode: str, multiple: bool
) -> tuple[list[dict[str, Any]], str]:
    """Return the study samples that belong to THIS analysis unit, and why.

    WHAT THIS ENDS. Metabolomics Workbench publishes its factor table at study level, so this
    adapter computed the sample list once and gave the same list to every analysis unit -- and
    said so, in a warning on every such unit: "Metabolomics Workbench factors are study-level;
    verify that each sample belongs to this analysis_id." Nothing ever verified it. Measured on
    2026-09-21: of 536 studies holding a campaign-eligible LC-MS unit, 291 had units whose file
    lists were byte-for-byte identical. ST003038's "DDA Positive" and "DDA Negative" units each
    held the same twenty files, ten named POS and ten named NEG, so either unit would have
    aligned both polarities together.

    The evidence to split them was already present and unused: every sample carries a raw_file
    name, and those names state the polarity. This uses that, the same way _partition_archives
    already uses it for archive names.

    WHAT IT WILL NOT DO. It never invents a split. If the file names do not state a polarity, if
    only one polarity is present, or if the unit itself is labelled Both or Unknown, the whole
    list is returned with a reason, and the caller marks the unit for review rather than
    recording a determination nothing supports.
    """
    if not multiple or ion_mode not in {"Positive", "Negative"}:
        return samples, ""
    labelled = [(item, file_polarity(item.get("raw_file", ""))) for item in samples]
    stated = {polarity for _, polarity in labelled if polarity}
    if not stated:
        return samples, "raw-file names state no ion mode, so samples could not be split by polarity"
    if len(stated) == 1:
        # Every file names the same polarity. Either the study really is single-polarity and the
        # unit list is already right, or the unit's own label disagrees with every file it holds.
        only = stated.pop()
        if only != ion_mode:
            return samples, (
                f"every raw-file name states {only} but this unit is labelled {ion_mode}"
            )
        return samples, ""
    matched = [item for item, polarity in labelled if polarity == ion_mode]
    unstated = [item for item, polarity in labelled if not polarity]
    if not matched:
        return samples, (
            f"the study holds both polarities and none of the raw-file names states {ion_mode}"
        )
    if unstated:
        return samples, (
            f"{len(unstated)} of {len(samples)} raw-file names state no ion mode, so the split "
            "would silently drop them"
        )
    return matched, ""


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

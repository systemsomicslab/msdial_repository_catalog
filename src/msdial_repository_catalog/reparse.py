"""Re-apply an adapter fix to stored payloads, without contacting any repository service.

WHY THIS EXISTS. A crawl skips a study whose source hash and parser version both match what is
already stored, which is right for bandwidth and wrong for a code change: an adapter fix does not
reach the records it was written for. The 2026-09-21 full re-crawl ran for twenty-five hours,
failed 875 MetaboLights accessions on HTTP 503, and re-applied nothing -- the remote payloads had
not changed, so every study came back "unchanged".

Bumping the parser version would force a re-ingest, and would still re-fetch 5,601 studies over the
network to receive bytes the catalog already holds. It holds them because every adapter stores its
raw responses in `repository_metadata`: Metabolomics Workbench keeps summary, analysis and factors;
MetaboLights keeps assay_listing, assays, study_api and study_table; MetaboBank keeps data_root,
filelist, sdrf_rows and search_entry.

WHAT THIS IS NOT. It is not a general re-parse. An adapter only re-applies what it can derive from
what was stored, and says so when it cannot. Metabolomics Workbench, for instance, also reads a
study's HTML detail page and its download page during a crawl and neither is stored, so every
inferred field -- acquisition mode, chromatography, target omics, untargeted -- is left exactly as
it was rather than re-derived from less evidence than the original crawl had. An adapter with no
local re-parse reports that plainly instead of rewriting a study from nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .adapters import native_adapter
from .normalize import project_to_study
from .storage import Catalog


@dataclass
class ReparseSummary:
    repository: str
    reparsed: int = 0
    unchanged: int = 0
    not_supported: int = 0
    failed: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "reparsed": self.reparsed,
            "unchanged": self.unchanged,
            "not_supported": self.not_supported,
            "failed": self.failed,
            "failures": self.failures[:50],
        }


def reparse_repository(
    catalog: Catalog,
    repository: str,
    *,
    parser_version: str,
    limit: int | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> ReparseSummary:
    """Rebuild every stored study of one repository through its adapter's local re-parse."""
    summary = ReparseSummary(repository=repository)
    adapter = native_adapter(repository)
    reparse = getattr(adapter, "reparse_units", None)
    accessions = catalog.accessions(repository)
    if limit is not None:
        accessions = accessions[: max(0, int(limit))]
    if reparse is None:
        summary.not_supported = len(accessions)
        return summary

    for index, accession in enumerate(accessions, start=1):
        if progress:
            progress(accession, index, len(accessions))
        try:
            payload = catalog.source_payload(repository, accession)
            units = reparse(payload)
            if units is None:
                summary.not_supported += 1
                continue
            rebuilt = {**payload, "analysis_units": units}
            if rebuilt == payload:
                summary.unchanged += 1
                continue
            study = project_to_study(rebuilt, parser_version)
            catalog.ingest_study(study)
            summary.reparsed += 1
        except Exception as error:  # One unreadable stored record must not stop the pass.
            summary.failed += 1
            summary.failures.append({"accession": accession, "error": str(error)})
    return summary


def reparse_catalog(
    catalog: Catalog,
    repositories: list[str] | tuple[str, ...],
    *,
    parser_version: str,
    limit: int | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, Any]:
    results = [
        reparse_repository(
            catalog, repository, parser_version=parser_version, limit=limit, progress=progress
        )
        for repository in repositories
    ]
    return {
        "parser_version": parser_version,
        "repositories": [item.as_dict() for item in results],
        "reparsed": sum(item.reparsed for item in results),
        "unchanged": sum(item.unchanged for item in results),
        "not_supported": sum(item.not_supported for item in results),
        "failed": sum(item.failed for item in results),
    }

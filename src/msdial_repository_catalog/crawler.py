from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from .models import StudyRecord, stable_id
from .normalize import project_to_study
from .storage import Catalog


class RepositoryAdapter(Protocol):
    name: str

    def list_accessions(self) -> list[str]: ...

    def inspect_metadata(self, accession: str) -> dict[str, Any] | StudyRecord: ...


@dataclass(slots=True)
class CrawlSummary:
    repository: str
    discovered: int = 0
    hydrated: int = 0
    unchanged: int = 0
    failed: int = 0
    cancelled: bool = False
    failures: list[dict[str, str]] = field(default_factory=list)


class CatalogCrawler:
    def __init__(self, catalog: Catalog, crawler_version: str = "0.2.0") -> None:
        self.catalog = catalog
        self.crawler_version = crawler_version

    def sync(
        self,
        adapter: RepositoryAdapter,
        accessions: list[str] | None = None,
        exclude_accessions: set[str] | None = None,
        limit: int | None = None,
        progress: Callable[[dict[str, Any]], None] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> CrawlSummary:
        _notify(progress, {"stage": "discovering", "repository": adapter.name})
        selected = list(accessions if accessions is not None else adapter.list_accessions())
        if exclude_accessions:
            selected = [value for value in selected if value not in exclude_accessions]
        if limit is not None:
            selected = selected[: max(0, int(limit))]
        summary = CrawlSummary(repository=adapter.name, discovered=len(selected))
        crawl_run_id = self.catalog.start_crawl(adapter.name, self.crawler_version, len(selected))
        _notify(progress, {
            "stage": "discovered", "repository": adapter.name,
            "completed": 0, "total": len(selected),
        })
        try:
            for index, accession in enumerate(selected):
                if cancel_requested is not None and cancel_requested():
                    summary.cancelled = True
                    break
                _notify(progress, {
                    "stage": "processing", "repository": adapter.name,
                    "accession": accession, "completed": index, "total": len(selected),
                    "hydrated": summary.hydrated, "unchanged": summary.unchanged,
                    "failed": summary.failed,
                })
                try:
                    payload = adapter.inspect_metadata(accession)
                    study = payload if isinstance(payload, StudyRecord) else project_to_study(payload, self.crawler_version)
                    state = self.catalog.source_state(study.repository, study.accession)
                    if (
                        state["source_hash"] == study.source_hash()
                        and state["parser_version"] == study.parser_version
                    ):
                        summary.unchanged += 1
                    else:
                        self.catalog.ingest_study(study)
                        summary.hydrated += 1
                except Exception as error:  # One broken public record must not stop a crawl.
                    summary.failed += 1
                    summary.failures.append({"accession": accession, "error": str(error)})
                _notify(progress, {
                    "stage": "item_completed", "repository": adapter.name,
                    "accession": accession, "completed": index + 1, "total": len(selected),
                    "hydrated": summary.hydrated, "unchanged": summary.unchanged,
                    "failed": summary.failed,
                })
        finally:
            self.catalog.finish_crawl(crawl_run_id, summary)
        _notify(progress, {
            "stage": "cancelled" if summary.cancelled else "completed",
            "repository": adapter.name, "completed": (
                summary.hydrated + summary.unchanged + summary.failed
            ), "total": len(selected), "hydrated": summary.hydrated,
            "unchanged": summary.unchanged, "failed": summary.failed,
        })
        return summary


def _notify(callback: Callable[[dict[str, Any]], None] | None, event: dict[str, Any]) -> None:
    if callback is not None:
        callback(event)


class JsonDirectoryAdapter:
    """Adapter for archived source payloads and existing Interactive exports."""

    def __init__(self, repository: str, root: str | Path) -> None:
        self.name = repository
        self.root = Path(root).expanduser().resolve()

    def list_accessions(self) -> list[str]:
        return sorted(path.stem for path in self.root.glob("*.json"))

    def inspect_metadata(self, accession: str) -> dict[str, Any]:
        payload = json.loads((self.root / f"{accession}.json").read_text(encoding="utf-8-sig"))
        payload.setdefault("repository", self.name)
        payload.setdefault("accession", accession)
        return payload

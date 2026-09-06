from __future__ import annotations

import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .adapters import NATIVE_ADAPTERS, native_adapter
from .crawler import CatalogCrawler
from .storage import Catalog


REPOSITORIES = tuple(NATIVE_ADAPTERS)
ACTIVE_STATES = {"queued", "running", "cancelling"}


class UpdateJobManager:
    """Run one bounded catalog update at a time and expose serializable progress."""

    def __init__(
        self,
        database: str | Path,
        *,
        adapter_factory: Callable[[str], Any] = native_adapter,
        crawler_version: str = "0.5.0",
    ) -> None:
        self.database = Path(database).expanduser().resolve()
        self.adapter_factory = adapter_factory
        self.crawler_version = crawler_version
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_monotonic = 0.0
        self._repository_base_totals = {"hydrated": 0, "unchanged": 0, "failed": 0}
        self._state = self._idle_state()

    def start(
        self,
        repositories: list[str] | tuple[str, ...] | None = None,
        *,
        mode: str = "indexed",
        limit: int | None = None,
    ) -> dict[str, Any]:
        selected = list(dict.fromkeys(repositories or REPOSITORIES))
        unknown = [name for name in selected if name not in REPOSITORIES]
        if unknown:
            raise ValueError(f"Unknown repository: {', '.join(unknown)}")
        if not selected:
            raise ValueError("Select at least one repository.")
        if mode not in {"indexed", "unindexed", "discover"}:
            raise ValueError("Update mode must be 'indexed', 'unindexed', or 'discover'.")
        if limit is not None and int(limit) < 1:
            raise ValueError("Limit must be one or greater when specified.")

        with self._lock:
            if self._state["state"] in ACTIVE_STATES:
                raise RuntimeError("A catalog update is already running.")
            self._cancel = threading.Event()
            self._started_monotonic = time.monotonic()
            now = _utc_now()
            self._state = {
                "job_id": uuid.uuid4().hex,
                "state": "queued",
                "mode": mode,
                "repositories": selected,
                "repository": "",
                "repository_position": 0,
                "repository_count": len(selected),
                "accession": "",
                "stage": "queued",
                "completed": 0,
                "total": 0,
                "percent": 0.0,
                "hydrated": 0,
                "unchanged": 0,
                "failed": 0,
                "elapsed_seconds": 0.0,
                "eta_seconds": None,
                "started_at": now,
                "completed_at": "",
                "message": "Catalog update queued.",
                "logs": [],
                "failures": [],
                "limit": int(limit) if limit is not None else None,
            }
            self._append_log(
                f"Starting {mode} update for {len(selected)} repository source(s)."
            )
            self._thread = threading.Thread(
                target=self._run,
                args=(selected, mode, int(limit) if limit is not None else None),
                name=f"catalog-update-{self._state['job_id'][:8]}",
                daemon=True,
            )
            self._thread.start()
            return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_timing()
            return deepcopy(self._state)

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            if self._state["state"] not in ACTIVE_STATES:
                return deepcopy(self._state)
            self._cancel.set()
            self._state["state"] = "cancelling"
            self._state["message"] = "Cancellation requested; finishing the current accession."
            self._append_log(self._state["message"])
            return deepcopy(self._state)

    def wait(self, timeout: float | None = None) -> dict[str, Any]:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.status()

    def _run(self, repositories: list[str], mode: str, limit: int | None) -> None:
        totals = {"hydrated": 0, "unchanged": 0, "failed": 0}
        try:
            self._set(state="running", stage="starting", message="Catalog update started.")
            for position, repository in enumerate(repositories, start=1):
                if self._cancel.is_set():
                    break
                with Catalog(self.database) as catalog:
                    indexed_accessions = catalog.accessions(repository)
                accessions = indexed_accessions if mode == "indexed" else None
                excluded = set(indexed_accessions) if mode == "unindexed" else None
                if mode == "indexed" and not accessions:
                    self._set(
                        repository=repository,
                        repository_position=position,
                        stage="skipped",
                        completed=0,
                        total=0,
                        accession="",
                        message=f"No indexed {repository} accessions to refresh.",
                    )
                    self._append_log(self._state["message"])
                    continue

                self._set(
                    repository=repository,
                    repository_position=position,
                    accession="",
                    stage="discovering" if mode in {"unindexed", "discover"} else "preparing",
                    completed=0,
                    total=len(accessions) if accessions is not None else 0,
                    message=(
                        f"Discovering unindexed {repository} accessions..."
                        if mode == "unindexed"
                        else f"Discovering and refreshing all {repository} accessions..."
                        if mode == "discover"
                        else f"Refreshing {len(accessions or [])} indexed {repository} accessions."
                    ),
                )
                with self._lock:
                    self._repository_base_totals = dict(totals)
                self._append_log(self._state["message"])
                with Catalog(self.database) as catalog:
                    summary = CatalogCrawler(catalog, self.crawler_version).sync(
                        self.adapter_factory(repository),
                        accessions=accessions,
                        exclude_accessions=excluded,
                        limit=limit,
                        progress=lambda event, p=position: self._on_progress(event, p),
                        cancel_requested=self._cancel.is_set,
                    )
                for key in totals:
                    totals[key] += int(getattr(summary, key))
                with self._lock:
                    self._state.update(totals)
                    self._state["failures"].extend(summary.failures)
                self._append_log(
                    f"Finished {repository}: {summary.hydrated} updated, "
                    f"{summary.unchanged} unchanged, {summary.failed} failed."
                )
                if summary.cancelled:
                    break

            if self._cancel.is_set():
                final_state = "cancelled"
                message = "Catalog update cancelled."
            elif totals["failed"]:
                final_state = "completed_with_errors"
                message = f"Catalog update completed with {totals['failed']} failed accession(s)."
            else:
                final_state = "completed"
                message = "Catalog update completed."
            self._finish(final_state, message)
        except Exception as error:
            self._append_log(f"Update failed: {error}")
            self._finish("failed", f"Catalog update failed: {error}")

    def _on_progress(self, event: dict[str, Any], repository_position: int) -> None:
        stage = str(event.get("stage") or "running")
        repository = str(event.get("repository") or "")
        accession = str(event.get("accession") or "")
        completed = int(event.get("completed") or 0)
        total = int(event.get("total") or 0)
        if stage == "discovering":
            message = f"Discovering {repository} accessions..."
        elif stage == "discovered":
            message = f"Found {total} {repository} accession(s) for this update."
            self._append_log(message)
        elif stage == "processing":
            message = f"Reading {repository} / {accession}"
        elif stage == "item_completed":
            message = f"Processed {completed} of {total} in {repository}."
        else:
            message = f"{repository}: {stage.replace('_', ' ')}."
        self._set(
            state="cancelling" if self._cancel.is_set() else "running",
            repository=repository,
            repository_position=repository_position,
            accession=accession,
            stage=stage,
            completed=completed,
            total=total,
            hydrated=self._repository_base_totals["hydrated"] + int(
                event.get("hydrated", self._state.get("hydrated", 0) - self._repository_base_totals["hydrated"])
            ),
            unchanged=self._repository_base_totals["unchanged"] + int(
                event.get("unchanged", self._state.get("unchanged", 0) - self._repository_base_totals["unchanged"])
            ),
            failed=self._repository_base_totals["failed"] + int(
                event.get("failed", self._state.get("failed", 0) - self._repository_base_totals["failed"])
            ),
            message=message,
        )

    def _set(self, **values: Any) -> None:
        with self._lock:
            self._state.update(values)
            self._refresh_timing()

    def _finish(self, state: str, message: str) -> None:
        with self._lock:
            self._state.update(
                state=state,
                stage=state,
                message=message,
                completed_at=_utc_now(),
                eta_seconds=0.0 if state in {"completed", "completed_with_errors"} else None,
            )
            self._refresh_timing(final=True)
            self._append_log(message)

    def _refresh_timing(self, final: bool = False) -> None:
        if not self._started_monotonic:
            return
        elapsed = max(0.0, time.monotonic() - self._started_monotonic)
        self._state["elapsed_seconds"] = round(elapsed, 1)
        repository_count = max(1, int(self._state.get("repository_count") or 1))
        position = max(0, int(self._state.get("repository_position") or 0))
        total = int(self._state.get("total") or 0)
        completed = int(self._state.get("completed") or 0)
        current_fraction = (completed / total) if total else 0.0
        fraction = min(1.0, max(0.0, ((position - 1) + current_fraction) / repository_count))
        if self._state.get("state") in {"completed", "completed_with_errors"}:
            fraction = 1.0
        self._state["percent"] = round(fraction * 100, 1)
        if final:
            return
        self._state["eta_seconds"] = (
            round(elapsed * (1.0 - fraction) / fraction, 1) if fraction > 0 else None
        )

    def _append_log(self, message: str) -> None:
        with self._lock:
            logs = self._state.setdefault("logs", [])
            logs.append(f"{datetime.now().astimezone().strftime('%H:%M:%S')}  {message}")
            del logs[:-30]

    @staticmethod
    def _idle_state() -> dict[str, Any]:
        return {
            "job_id": "", "state": "idle", "mode": "indexed", "repositories": [],
            "repository": "", "repository_position": 0, "repository_count": 0,
            "accession": "", "stage": "idle", "completed": 0, "total": 0,
            "percent": 0.0, "hydrated": 0, "unchanged": 0, "failed": 0,
            "elapsed_seconds": 0.0, "eta_seconds": None, "started_at": "",
            "completed_at": "", "message": "No catalog update is running.",
            "logs": [], "failures": [], "limit": None,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

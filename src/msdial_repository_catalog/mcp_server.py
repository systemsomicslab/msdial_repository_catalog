from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable, TypeVar

from .class_proposal import (
    analysis_samples,
    build_class_proposal_request,
    field_based_proposal,
    normalize_file_roles,
    validate_class_proposal,
)
from .models import ClassAssignment, ClassProposal, stable_id
from .storage import Catalog
from .update_jobs import REPOSITORIES, UpdateJobManager


DEFAULT_DATABASE = Path(
    os.environ.get(
        "MSDIAL_REPOSITORY_CATALOG",
        str(Path.home() / ".msdial" / "repository-catalog.sqlite"),
    )
).expanduser()

try:
    from mcp.server import MCPServer

    mcp = MCPServer("MS-DIAL Repository Metadata Catalog")
except ImportError:
    mcp = None

F = TypeVar("F", bound=Callable[..., Any])
_UPDATE_MANAGERS: dict[Path, UpdateJobManager] = {}
_UPDATE_MANAGERS_LOCK = threading.Lock()


def tool() -> Callable[[F], F]:
    if mcp is None:
        return lambda function: function
    return mcp.tool()


def _database(value: str = "") -> Path:
    return Path(value).expanduser().resolve() if value.strip() else DEFAULT_DATABASE.resolve()


def _update_manager(database: str = "") -> UpdateJobManager:
    path = _database(database)
    with _UPDATE_MANAGERS_LOCK:
        return _UPDATE_MANAGERS.setdefault(path, UpdateJobManager(path))


@tool()
def msdial_catalog_status(database: str = "") -> dict[str, Any]:
    """Report the local catalog path, schema, and indexed record counts."""
    with Catalog(_database(database)) as catalog:
        return catalog.stats()


@tool()
def msdial_catalog_update_start(
    repositories: list[str] | None = None,
    mode: str = "indexed",
    limit: int | None = None,
    confirmed: bool = False,
    database: str = "",
) -> dict[str, Any]:
    """Start a metadata-only catalog update; public repository services are contacted."""
    selected = repositories or list(REPOSITORIES)
    if not confirmed:
        return {
            "confirmation_required": True,
            "message": (
                "This contacts public repository services. Confirm the repository list and "
                "whether the scope is indexed, unindexed-only, or discover-and-refresh-all."
            ),
            "repositories": selected,
            "mode": mode,
            "limit": limit,
        }
    return _update_manager(database).start(selected, mode=mode, limit=limit)


@tool()
def msdial_catalog_update_status(database: str = "") -> dict[str, Any]:
    """Report progress, elapsed time, ETA, and outcomes for the current update job."""
    return _update_manager(database).status()


@tool()
def msdial_catalog_update_cancel(database: str = "") -> dict[str, Any]:
    """Request cancellation after the accession currently being read finishes."""
    return _update_manager(database).cancel()


@tool()
def msdial_catalog_storage_report(database: str = "") -> dict[str, Any]:
    """Report local database size and compressed source-payload storage use."""
    with Catalog(_database(database)) as catalog:
        return catalog.storage_report()


@tool()
def msdial_catalog_search(
    text: str = "",
    repository: str = "",
    accessions: list[str] | None = None,
    separation: str = "",
    chromatography: str = "",
    ion_mode: str = "",
    acquisition_mode: str = "",
    target_omics: str = "",
    biological_context: str = "",
    review_status: str = "",
    max_download_gb: float | None = None,
    limit: int = 50,
    database: str = "",
) -> dict[str, Any]:
    """Search local analysis units without contacting a public repository or using an LLM."""
    with Catalog(_database(database)) as catalog:
        matches = catalog.search(
            text=text,
            repository=repository,
            accessions=accessions,
            separation=separation,
            chromatography=chromatography,
            ion_mode=ion_mode,
            acquisition_mode=acquisition_mode,
            target_omics=target_omics,
            biological_context=biological_context,
            review_status=review_status,
            max_download_bytes=(
                int(max_download_gb * 1024**3) if max_download_gb is not None else None
            ),
            limit=limit,
        )
    requested = [str(item).strip() for item in (accessions or []) if str(item).strip()]
    found = sorted({str(item["accession"]) for item in matches})
    return {
        "query": {
            "text": text,
            "repository": repository,
            "accessions": requested,
            "biological_context": biological_context,
        },
        "matches": matches,
        "accessions_found": found,
        "accessions_missing": [item for item in requested if item not in found],
    }


@tool()
def msdial_catalog_get_analysis_unit(
    unit_id: str,
    database: str = "",
    include_samples: bool = False,
    sample_limit: int = 0,
    include_files: bool = False,
    file_limit: int = 0,
) -> dict[str, Any]:
    """Return one bounded MS-DIAL-compatible analysis unit with its sample and file manifests.

    Samples and files are both written beside the database and named by path
    rather than inlined. A 200-file unit returned 57,647 characters, 99.6% of it
    the file list, which no caller could receive.
    """
    database_path = _database(database)
    with Catalog(database_path) as catalog:
        unit = catalog.get_unit(unit_id)
    samples = list(unit.get("samples", []))
    files = list(unit.get("files", []))
    handoffs = database_path.parent / "handoffs"
    handoffs.mkdir(parents=True, exist_ok=True)
    sample_path = handoffs / f"{unit_id}-samples.json"
    file_path = handoffs / f"{unit_id}-files.json"
    sample_path.write_text(
        json.dumps(samples, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    file_path.write_text(
        json.dumps(files, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    response = dict(unit)
    response["sample_count"] = len(samples)
    response["sample_table_path"] = str(sample_path.resolve())
    response["file_count"] = len(files)
    response["file_manifest_path"] = str(file_path.resolve())
    if include_samples:
        limit = max(0, int(sample_limit))
        response["samples"] = samples[:limit] if limit else samples
        response["samples_truncated"] = bool(limit and len(samples) > limit)
    else:
        response["samples"] = []
        response["samples_omitted"] = True
    if include_files:
        limit = max(0, int(file_limit))
        response["files"] = files[:limit] if limit else files
        response["files_truncated"] = bool(limit and len(files) > limit)
    else:
        response["files"] = []
        response["files_omitted"] = True
    return response


@tool()
def msdial_catalog_class_request(
    unit_id: str,
    purpose: str,
    database: str = "",
    include_samples: bool = False,
    sample_limit: int = 0,
) -> dict[str, Any]:
    """Build the bounded metadata request used by an agent to propose Class and contrasts."""
    database_path = _database(database)
    with Catalog(database_path) as catalog:
        unit = catalog.get_unit(unit_id)
    unit = {**unit, "samples": analysis_samples(unit)}
    request = build_class_proposal_request(
        unit,
        purpose,
        include_samples=include_samples,
        sample_limit=max(0, int(sample_limit)),
    )
    sample_path = database_path.parent / "handoffs" / f"{unit_id}-samples.json"
    sample_path.parent.mkdir(parents=True, exist_ok=True)
    sample_path.write_text(
        json.dumps(unit.get("samples", []), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    request["sample_table_path"] = str(sample_path.resolve())
    return request


@tool()
def msdial_catalog_save_class_proposal(
    unit_id: str,
    purpose: str,
    selected_fields: list[str],
    assignments_json: str,
    rationale: str,
    contrast_definition_json: str = "{}",
    model: str = "agent",
    confirmed: bool = False,
    database: str = "",
) -> dict[str, Any]:
    """Validate and save an agent Class proposal only after explicit user confirmation."""
    assignments_payload = json.loads(assignments_json) if assignments_json.strip() else []
    contrast = json.loads(contrast_definition_json or "{}")
    with Catalog(_database(database)) as catalog:
        unit = catalog.get_unit(unit_id)
    if not assignments_payload:
        proposal = field_based_proposal(unit, purpose, selected_fields, rationale)
        proposal.contrast_definition = contrast
        proposal.model = model or proposal.model
        if not confirmed:
            counts: dict[str, int] = {}
            for item in proposal.assignments:
                counts[item.class_label] = counts.get(item.class_label, 0) + 1
            return {
                "confirmation_required": True,
                "proposal_preview": {
                    "unit_id": unit_id,
                    "selected_fields": selected_fields,
                    "class_counts": counts,
                    "assignment_count": len(proposal.assignments),
                    "rationale": rationale,
                    "contrast_definition": contrast,
                },
                "message": "Review the deterministic field projection before saving it.",
            }
        with Catalog(_database(database)) as catalog:
            catalog.save_class_proposal(proposal)
        return {"saved": True, "proposal": proposal.as_dict()}
    if not confirmed:
        return {
            "confirmation_required": True,
            "message": "Review the selected fields, every sample assignment, rationale, and contrast before saving.",
        }
    assignments = [
        ClassAssignment(
            sample_id=str(item["sample_id"]),
            class_label=str(item["class_label"]),
            values={str(key): str(value) for key, value in dict(item.get("values") or {}).items()},
        )
        for item in assignments_payload
    ]
    prompt_payload = json.dumps(
        {
            "unit_id": unit_id,
            "purpose": purpose,
            "selected_fields": selected_fields,
            "assignments": assignments_payload,
            "rationale": rationale,
            "contrast": contrast,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    proposal = ClassProposal(
        proposal_id=stable_id("agent-class-proposal", prompt_payload),
        unit_id=unit_id,
        purpose=purpose,
        selected_fields=selected_fields,
        assignments=assignments,
        rationale=rationale,
        contrast_definition=contrast,
        model=model,
        prompt_hash=hashlib.sha256(prompt_payload.encode("utf-8")).hexdigest(),
    )
    with Catalog(_database(database)) as catalog:
        validate_class_proposal(unit, proposal)
        catalog.save_class_proposal(proposal)
    return {"saved": True, "proposal": proposal.as_dict()}


@tool()
def msdial_catalog_reanalysis_handoff(
    unit_id: str,
    class_proposal_id: str = "",
    database: str = "",
) -> dict[str, Any]:
    """Create a structured handoff for MS-DIAL Interactive without downloading raw data."""
    database_path = _database(database)
    with Catalog(database_path) as catalog:
        unit = catalog.get_unit(unit_id)
        proposal = catalog.get_class_proposal(class_proposal_id) if class_proposal_id else None
        scope = catalog.download_scope(
            [str(item.get("download_url") or "") for item in unit["files"]]
        )
    required_review = [
        field
        for field in ("separation", "ion_mode", "acquisition_mode")
        if str(unit.get(field) or "Unknown") == "Unknown"
    ]
    blocking_reasons = [f"technical_metadata:{field}" for field in required_review]
    if proposal is None:
        blocking_reasons.append("class_proposal:missing")
    files = _handoff_files(unit["files"])
    primary_files = [item for item in files if item.get("role", "raw") == "raw"]
    analytical_samples = {
        str(item.get("sample_id") or item.get("path") or "") for item in primary_files
    }
    urls = {str(item.get("download_url") or "") for item in files if item.get("download_url")}
    bundle_level = unit["repository"] == "mb_post" or len(urls) < len(files)
    analysis_unit = {**unit, "files": files}
    analysis_rows = analysis_samples(analysis_unit)
    samples, unit_attributes = _compact_sample_metadata(analysis_rows)
    sample_path = database_path.parent / "handoffs" / f"{unit_id}-samples.json"
    file_path = database_path.parent / "handoffs" / f"{unit_id}-files.json"
    sample_path.parent.mkdir(parents=True, exist_ok=True)
    sample_path.write_text(
        json.dumps(samples, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    file_path.write_text(
        json.dumps(files, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    payload = {
        "schema": "msdial-repository-reanalysis-handoff.v1",
        "repository": unit["repository"],
        "accession": unit["accession"],
        "analysis_unit_id": unit_id,
        "source_subrecord_id": unit["source_subrecord_id"],
        "title": unit.get("title", ""),
        "description": unit.get("description", ""),
        "publications": unit.get("publications", []),
        "publication_status": _publication_status(unit),
        "technical_settings": {
            key: unit.get(key)
            for key in (
                "separation", "chromatography", "ion_mode", "acquisition_mode",
                "ion_mobility", "instrument", "target_omics", "untargeted",
            )
        },
        "repository_url": unit.get("public_url", ""),
        "files": files,
        "file_manifest_path": str(file_path.resolve()),
        "download_scope": {
            "kind": "accession_bundle_with_file_allowlist" if bundle_level else "unit_files",
            "url_scope": "accession" if bundle_level else "file",
            "allowlist_required": bundle_level,
            "allowlist_keys": ["path", "checksum"],
            "file_count": len(files),
            "analysis_file_count": len(primary_files),
            "total_file_bytes": sum(int(item.get("size_bytes") or 0) for item in files),
            "bundle_bytes": scope["bundle_bytes"],
            "bundle_shared_unit_count": scope["bundle_shared_unit_count"],
            "bundle_urls": scope["urls"],
            "note": (
                "Download URLs may resolve to an accession bundle. Retain only paths listed "
                "in files and verify checksums when supplied."
                if bundle_level else "Each URL represents a unit-scoped file."
            ),
        },
        "sample_count": len(analysis_rows),
        "analytical_sample_count": len(analytical_samples),
        "unit_attributes": unit_attributes,
        "sample_table_path": str(sample_path.resolve()),
        "sample_metadata": samples,
        "class_proposal": proposal,
        "review_status": unit["review_status"],
        "required_review": required_review,
        "blocking_reasons": blocking_reasons,
        "ready_for_download_planning": not blocking_reasons,
        "next_action": (
            "Pass this handoff to MS-DIAL Interactive for bounded download planning."
            if not required_review and proposal is not None
            else "Resolve required technical metadata and confirm a Class proposal first."
        ),
    }
    handoff_path = database_path.parent / "handoffs" / f"{unit_id}.json"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    response = dict(payload)
    response["handoff_path"] = str(handoff_path.resolve())
    response["sample_metadata"] = []
    response["sample_metadata_omitted"] = True
    response["files"] = []
    response["files_omitted"] = True
    return response


def _publication_status(unit: dict[str, Any]) -> str:
    if unit.get("publications"):
        return "recorded"
    warnings = " ".join(str(item) for item in unit.get("warnings", [])).casefold()
    if any(token in warnings for token in ("metadata detail was unavailable", "metadata unavailable", "lookup failed")):
        return "not_retrieved"
    return "none_recorded"


def _compact_sample_metadata(
    samples: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not samples:
        return [], {}
    common_keys = set(samples[0].get("attributes", {}))
    for sample in samples[1:]:
        common_keys &= set(sample.get("attributes", {}))
    constants = {
        key: samples[0]["attributes"][key]
        for key in common_keys
        if all(
            sample.get("attributes", {}).get(key) == samples[0]["attributes"][key]
            for sample in samples
        )
    }
    compact = [
        {
            "sample_id": sample["sample_id"],
            "raw_file": sample.get("raw_file", ""),
            "related_files": list(sample.get("related_files", [])),
            "attributes": {
                key: value
                for key, value in sample.get("attributes", {}).items()
                if key not in constants
            },
        }
        for sample in samples
    ]
    return compact, constants


def _handoff_files(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return normalize_file_roles(files)


def main() -> None:
    if mcp is None:
        raise RuntimeError(
            'The MCP SDK is required. Install this package with: python -m pip install -e ".[mcp]"'
        )
    mcp.run()


if __name__ == "__main__":
    main()

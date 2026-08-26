from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, TypeVar

from .class_proposal import build_class_proposal_request, validate_class_proposal
from .models import ClassAssignment, ClassProposal, stable_id
from .storage import Catalog


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


def tool() -> Callable[[F], F]:
    if mcp is None:
        return lambda function: function
    return mcp.tool()


def _database(value: str = "") -> Path:
    return Path(value).expanduser().resolve() if value.strip() else DEFAULT_DATABASE.resolve()


@tool()
def msdial_catalog_status(database: str = "") -> dict[str, Any]:
    """Report the local catalog path, schema, and indexed record counts."""
    with Catalog(_database(database)) as catalog:
        return catalog.stats()


@tool()
def msdial_catalog_search(
    text: str = "",
    repository: str = "",
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
    return {"query": {"text": text, "biological_context": biological_context}, "matches": matches}


@tool()
def msdial_catalog_get_analysis_unit(unit_id: str, database: str = "") -> dict[str, Any]:
    """Return one MS-DIAL-compatible analysis unit with source metadata and evidence."""
    with Catalog(_database(database)) as catalog:
        return catalog.get_unit(unit_id)


@tool()
def msdial_catalog_class_request(
    unit_id: str,
    purpose: str,
    database: str = "",
) -> dict[str, Any]:
    """Build the bounded metadata request used by an agent to propose Class and contrasts."""
    with Catalog(_database(database)) as catalog:
        return build_class_proposal_request(catalog.get_unit(unit_id), purpose)


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
    if not confirmed:
        return {
            "confirmation_required": True,
            "message": "Review the selected fields, every sample assignment, rationale, and contrast before saving.",
        }
    assignments_payload = json.loads(assignments_json)
    contrast = json.loads(contrast_definition_json or "{}")
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
        unit = catalog.get_unit(unit_id)
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
    with Catalog(_database(database)) as catalog:
        unit = catalog.get_unit(unit_id)
        proposal = catalog.get_class_proposal(class_proposal_id) if class_proposal_id else None
    required_review = [
        field
        for field in ("separation", "ion_mode", "acquisition_mode")
        if str(unit.get(field) or "Unknown") == "Unknown"
    ]
    return {
        "schema": "msdial-repository-reanalysis-handoff.v1",
        "repository": unit["repository"],
        "accession": unit["accession"],
        "analysis_unit_id": unit_id,
        "source_subrecord_id": unit["source_subrecord_id"],
        "technical_settings": {
            key: unit.get(key)
            for key in (
                "separation", "chromatography", "ion_mode", "acquisition_mode",
                "ion_mobility", "instrument", "target_omics", "untargeted",
            )
        },
        "repository_url": unit.get("public_url", ""),
        "files": unit["files"],
        "sample_metadata": [
            {
                "sample_id": sample["sample_id"],
                "raw_file": sample["raw_file"],
                "attributes": sample["attributes"],
            }
            for sample in unit["samples"]
        ],
        "class_proposal": proposal,
        "review_status": unit["review_status"],
        "required_review": required_review,
        "ready_for_download_planning": not required_review and proposal is not None,
        "next_action": (
            "Pass this handoff to MS-DIAL Interactive for bounded download planning."
            if not required_review and proposal is not None
            else "Resolve required technical metadata and confirm a Class proposal first."
        ),
    }


def main() -> None:
    if mcp is None:
        raise RuntimeError(
            'The MCP SDK is required. Install this package with: python -m pip install -e ".[mcp]"'
        )
    mcp.run()


if __name__ == "__main__":
    main()

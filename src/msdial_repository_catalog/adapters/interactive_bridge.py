from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


class InteractiveAdapterBridge:
    """Transitional bridge to the four adapters already validated in Interactive."""

    def __init__(self, repository: str, interactive_app_root: str | Path = "") -> None:
        if interactive_app_root:
            root = str(Path(interactive_app_root).expanduser().resolve())
            if root not in sys.path:
                sys.path.insert(0, root)
        try:
            from msdial_app.repository_reanalysis import ADAPTERS
        except ImportError as error:
            raise RuntimeError(
                "MS-DIAL Interactive is not importable. Install it or supply --interactive-app-root."
            ) from error
        if repository not in ADAPTERS:
            raise ValueError(f"Unknown repository: {repository}")
        self.name = repository
        self._adapter = ADAPTERS[repository]()

    def list_accessions(self) -> list[str]:
        return self._adapter.list_accessions()

    def inspect_metadata(self, accession: str) -> dict[str, Any]:
        value = self._adapter.inspect_metadata(accession)
        return value.as_dict() if hasattr(value, "as_dict") else dict(value)

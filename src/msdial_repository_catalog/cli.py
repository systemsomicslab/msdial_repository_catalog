from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .class_proposal import build_class_proposal_request, field_based_proposal
from .crawler import CatalogCrawler
from .normalize import project_to_study
from .storage import Catalog


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local MS-DIAL public repository metadata catalog")
    parser.add_argument("--database", default="catalog-data/msdial-repository-catalog.sqlite")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create or migrate the local SQLite catalog")

    ingest = commands.add_parser("ingest-json", help="Ingest normalized project JSON files")
    ingest.add_argument("paths", nargs="+")

    crawl = commands.add_parser(
        "crawl-interactive", help="Crawl through the validated MS-DIAL Interactive adapters"
    )
    crawl.add_argument(
        "repository",
        choices=["metabolomics_workbench", "metabolights", "mb_post", "metabobank"],
    )
    crawl.add_argument("--accession", action="append")
    crawl.add_argument("--limit", type=int)
    crawl.add_argument("--interactive-app-root", default="")

    search = commands.add_parser("search", help="Search local analysis units")
    for name in (
        "text", "repository", "separation", "chromatography", "ion-mode",
        "acquisition-mode", "target-omics", "biological-context", "review-status",
    ):
        search.add_argument(f"--{name}", default="")
    search.add_argument("--max-download-gb", type=float)
    search.add_argument("--limit", type=int, default=50)

    show = commands.add_parser("show-unit", help="Show one analysis unit and its samples")
    show.add_argument("unit_id")

    request = commands.add_parser("class-request", help="Create an agent-readable Class proposal request")
    request.add_argument("unit_id")
    request.add_argument("--purpose", required=True)

    proposal = commands.add_parser("propose-fields", help="Create and save a deterministic Class proposal")
    proposal.add_argument("unit_id")
    proposal.add_argument("--purpose", required=True)
    proposal.add_argument("--field", action="append", required=True)

    snapshot = commands.add_parser("snapshot", help="Create a compressed release snapshot and manifest")
    snapshot.add_argument("output")
    snapshot.add_argument("--include-local-decisions", action="store_true")

    args = parser.parse_args(argv)
    with Catalog(args.database) as catalog:
        catalog.initialize()
        if args.command == "init":
            result: Any = {"database": str(catalog.path), "schema": 1, "fts": catalog.fts_enabled}
        elif args.command == "ingest-json":
            result = []
            for value in args.paths:
                path = Path(value).expanduser().resolve()
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
                result.append(catalog.ingest_study(project_to_study(payload)))
        elif args.command == "crawl-interactive":
            from .adapters import InteractiveAdapterBridge

            summary = CatalogCrawler(catalog).sync(
                InteractiveAdapterBridge(args.repository, args.interactive_app_root),
                accessions=args.accession,
                limit=args.limit,
            )
            result = {
                "repository": summary.repository,
                "discovered": summary.discovered,
                "hydrated": summary.hydrated,
                "unchanged": summary.unchanged,
                "failed": summary.failed,
                "failures": summary.failures,
            }
        elif args.command == "search":
            result = catalog.search(
                text=args.text,
                repository=args.repository,
                separation=args.separation,
                chromatography=args.chromatography,
                ion_mode=args.ion_mode,
                acquisition_mode=args.acquisition_mode,
                target_omics=args.target_omics,
                biological_context=args.biological_context,
                review_status=args.review_status,
                max_download_bytes=(
                    int(args.max_download_gb * 1024**3) if args.max_download_gb is not None else None
                ),
                limit=args.limit,
            )
        elif args.command == "show-unit":
            result = catalog.get_unit(args.unit_id)
        elif args.command == "class-request":
            result = build_class_proposal_request(catalog.get_unit(args.unit_id), args.purpose)
        elif args.command == "propose-fields":
            proposal_value = field_based_proposal(
                catalog.get_unit(args.unit_id), args.purpose, list(args.field)
            )
            catalog.save_class_proposal(proposal_value)
            result = proposal_value.as_dict()
        elif args.command == "snapshot":
            result = catalog.snapshot(args.output, args.include_local_decisions)
        else:
            parser.error(f"Unknown command: {args.command}")
            return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

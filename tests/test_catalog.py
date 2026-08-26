from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from msdial_repository_catalog.class_proposal import (
    build_class_proposal_request,
    field_based_proposal,
)
from msdial_repository_catalog.crawler import CatalogCrawler
from msdial_repository_catalog.normalize import project_to_study
from msdial_repository_catalog.storage import Catalog


def mixed_project() -> dict:
    common_samples = [
        {
            "sample_id": "young_wt_1",
            "raw_file": "young_wt_1.raw",
            "values": {
                "Species": "Mus musculus",
                "Tissue": "liver",
                "Age group": "young",
                "Disease": "healthy",
                "Genotype": "WT",
                "Treatment": "vehicle",
                "Sex": "female",
                "Batch ID": "1",
            },
        },
        {
            "sample_id": "aged_ko_1",
            "raw_file": "aged_ko_1.raw",
            "values": {
                "Species": "Mus musculus",
                "Tissue": "liver",
                "Age group": "aged",
                "Disease": "inflammatory disease",
                "Genotype": "KO",
                "Treatment": "LPS",
                "Sex": "female",
                "Batch ID": "1",
            },
        },
    ]
    return {
        "repository": "mb_post",
        "accession": "MPST-CONTEXT-TEST",
        "title": "Lipid response in aging and inflammation",
        "description": "Mouse liver lipidomics under inflammatory stress",
        "public_url": "https://example.org/MPST-CONTEXT-TEST",
        "metadata_sources": ["https://example.org/api/MPST-CONTEXT-TEST"],
        "analysis_units": [
            {
                "source_subrecord_id": "lcms-neg-dda",
                "label": "RP negative DDA",
                "separation": "LC-MS",
                "chromatography": "Reversed phase",
                "ion_mode": "Negative",
                "acquisition_mode": "DDA",
                "target_omics": "Lipidomics",
                "untargeted": True,
                "review_status": "reviewed",
                "sample_metadata": common_samples,
                "files": [
                    {"name": row["raw_file"], "size_bytes": 1024, "role": "raw"}
                    for row in common_samples
                ],
            },
            {
                "source_subrecord_id": "lcms-pos-dia",
                "label": "HILIC positive DIA",
                "separation": "LC-MS",
                "chromatography": "HILIC",
                "ion_mode": "Positive",
                "acquisition_mode": "DIA",
                "target_omics": "Metabolomics",
                "untargeted": True,
                "review_status": "needs_review",
                "sample_metadata": [
                    {**row, "raw_file": row["raw_file"].replace(".raw", "_pos.raw")}
                    for row in common_samples
                ],
                "files": [
                    {"name": row["raw_file"].replace(".raw", "_pos.raw"), "size_bytes": 2048}
                    for row in common_samples
                ],
            },
        ],
    }


class CatalogTests(unittest.TestCase):
    def test_consistent_technical_sample_metadata_enriches_legacy_unit(self) -> None:
        payload = mixed_project()
        payload.pop("analysis_units")
        payload.update(
            {
                "separation": "LC-MS",
                "ion_mode": "Negative",
                "acquisition_mode": "DDA",
                "target_omics": "Lipidomics",
                "sample_metadata": [
                    {
                        "sample_id": "S1",
                        "raw_file": "S1.raw",
                        "values": {
                            "analyticalCondition / Chromatography type": "LC_Reversed phase",
                            "analyticalCondition / Instrument": "Vendor QTOF",
                            "analyticalCondition / Ion mobility": "No",
                        },
                    },
                    {
                        "sample_id": "S2",
                        "raw_file": "S2.raw",
                        "values": {
                            "analyticalCondition / Chromatography type": "LC_Reversed phase",
                            "analyticalCondition / Instrument": "Vendor QTOF",
                            "analyticalCondition / Ion mobility": "No",
                        },
                    },
                ],
            }
        )
        unit = project_to_study(payload).analysis_units[0]
        self.assertEqual("Reversed phase", unit.chromatography)
        self.assertEqual("Vendor QTOF", unit.instrument)
        self.assertEqual("Disabled", unit.ion_mobility)
        self.assertEqual(3, sum(item.method == "consistent-sample-metadata" for item in unit.evidence))

    def test_mixed_accession_becomes_multiple_analysis_units(self) -> None:
        study = project_to_study(mixed_project())
        self.assertEqual(2, len(study.analysis_units))
        self.assertNotEqual(study.analysis_units[0].unit_id, study.analysis_units[1].unit_id)
        self.assertEqual("Negative", study.analysis_units[0].ion_mode)
        categories = {item.category for item in study.analysis_units[0].samples[1].contexts}
        self.assertTrue({"species", "organ", "aging", "disease", "genotype", "intervention"} <= categories)
        process_values = {
            item.normalized_value
            for item in study.analysis_units[0].samples[1].contexts
            if item.category == "biological_process"
        }
        self.assertIn("aging", process_values)
        self.assertIn("inflammation", process_values)
        self.assertFalse(
            any(item.category == "study_topic" for item in study.analysis_units[0].samples[0].contexts)
        )
        self.assertIn(
            "aging",
            {item.normalized_value for item in study.analysis_units[0].contexts},
        )

    def test_catalog_search_uses_technical_and_biological_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "catalog.sqlite"
            with Catalog(database) as catalog:
                result = catalog.ingest_study(project_to_study(mixed_project()))
                self.assertEqual(2, result["analysis_units"])
                matches = catalog.search(
                    separation="LC-MS",
                    chromatography="Reversed phase",
                    ion_mode="Negative",
                    acquisition_mode="DDA",
                    target_omics="Lipidomics",
                    biological_context="aging",
                )
                self.assertEqual(1, len(matches))
                self.assertEqual("lcms-neg-dda", matches[0]["source_subrecord_id"])
                self.assertEqual(2048, matches[0]["download_bytes"])

    def test_class_request_and_saved_proposal_are_purpose_specific(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Catalog(Path(temporary) / "catalog.sqlite") as catalog:
                study = project_to_study(mixed_project())
                catalog.ingest_study(study)
                unit = catalog.get_unit(study.analysis_units[0].unit_id)
                request = build_class_proposal_request(
                    unit, "Compare inflammatory aging in KO and WT mouse liver."
                )
                fields = {item["field"] for item in request["candidate_fields"]}
                self.assertIn("Age group", fields)
                self.assertIn("Genotype", fields)
                self.assertIn(
                    request["candidate_fields"][0]["field"],
                    {"Age group", "Disease", "Genotype", "Treatment"},
                )
                proposal = field_based_proposal(
                    unit,
                    request["purpose"],
                    ["Age group", "Genotype"],
                    "Age and genotype define the requested biological contrast.",
                )
                catalog.save_class_proposal(proposal)
                labels = {item.class_label for item in proposal.assignments}
                self.assertEqual({"young_WT", "aged_KO"}, labels)

    def test_snapshot_has_checksum_and_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with Catalog(root / "catalog.sqlite") as catalog:
                catalog.ingest_study(project_to_study(mixed_project()))
                manifest = catalog.snapshot(root / "catalog.sqlite.gz")
            self.assertEqual(1, manifest["study_count"])
            self.assertEqual(2, manifest["analysis_unit_count"])
            self.assertEqual(64, len(manifest["sha256"]))
            self.assertTrue(Path(manifest["manifest_path"]).is_file())


class MemoryAdapter:
    name = "memory"

    def list_accessions(self) -> list[str]:
        return ["MPST-CONTEXT-TEST"]

    def inspect_metadata(self, accession: str) -> dict:
        return mixed_project()


class CrawlerTests(unittest.TestCase):
    def test_incremental_crawl_skips_unchanged_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Catalog(Path(temporary) / "catalog.sqlite") as catalog:
                crawler = CatalogCrawler(catalog)
                first = crawler.sync(MemoryAdapter())
                second = crawler.sync(MemoryAdapter())
                self.assertEqual(1, first.hydrated)
                self.assertEqual(1, second.unchanged)


if __name__ == "__main__":
    unittest.main()

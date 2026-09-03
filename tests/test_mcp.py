from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from msdial_repository_catalog.mcp_server import (
    msdial_catalog_class_request,
    msdial_catalog_get_analysis_unit,
    msdial_catalog_reanalysis_handoff,
    msdial_catalog_save_class_proposal,
    msdial_catalog_search,
    msdial_catalog_storage_report,
    msdial_catalog_status,
)
from msdial_repository_catalog.normalize import project_to_study
from msdial_repository_catalog.storage import Catalog

from test_catalog import mixed_project


class McpCatalogTests(unittest.TestCase):
    def test_local_search_class_confirmation_and_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = str(Path(temporary) / "catalog.sqlite")
            study = project_to_study(mixed_project())
            unit_id = study.analysis_units[0].unit_id
            with Catalog(database) as catalog:
                catalog.ingest_study(study)

            status = msdial_catalog_status(database)
            self.assertEqual(2, status["analysis_units"])
            storage = msdial_catalog_storage_report(database)
            self.assertEqual(1, storage["source_blobs"])
            self.assertGreater(storage["source_compressed_bytes"], 0)
            result = msdial_catalog_search(
                target_omics="Lipidomics",
                biological_context="inflammation",
                accessions=["MPST-CONTEXT-TEST", "MPST-MISSING"],
                database=database,
            )
            self.assertEqual(unit_id, result["matches"][0]["unit_id"])
            self.assertEqual(["MPST-CONTEXT-TEST"], result["accessions_found"])
            self.assertEqual(["MPST-MISSING"], result["accessions_missing"])
            request = msdial_catalog_class_request(
                unit_id, "Compare age and genotype.", database
            )
            self.assertNotIn("samples", request)
            self.assertEqual(2, request["sample_count"])
            self.assertTrue(Path(request["sample_table_path"]).is_file())

            deterministic_preview = msdial_catalog_save_class_proposal(
                unit_id,
                "Compare age and genotype.",
                ["Age group", "Genotype"],
                "",
                "Project the reviewed fields deterministically.",
                confirmed=False,
                database=database,
            )
            self.assertEqual(2, deterministic_preview["proposal_preview"]["assignment_count"])

            assignments = [
                {
                    "sample_id": "young_wt_1",
                    "class_label": "young_WT",
                    "values": {"Age group": "young", "Genotype": "WT"},
                },
                {
                    "sample_id": "aged_ko_1",
                    "class_label": "aged_KO",
                    "values": {"Age group": "aged", "Genotype": "KO"},
                },
            ]
            preview = msdial_catalog_save_class_proposal(
                unit_id,
                "Compare age and genotype.",
                ["Age group", "Genotype"],
                json.dumps(assignments),
                "Age and genotype define the requested contrast.",
                confirmed=False,
                database=database,
            )
            self.assertTrue(preview["confirmation_required"])
            saved = msdial_catalog_save_class_proposal(
                unit_id,
                "Compare age and genotype.",
                ["Age group", "Genotype"],
                json.dumps(assignments),
                "Age and genotype define the requested contrast.",
                json.dumps({"primary": "aged_KO vs young_WT"}),
                model="test-agent",
                confirmed=True,
                database=database,
            )
            handoff = msdial_catalog_reanalysis_handoff(
                unit_id, saved["proposal"]["proposal_id"], database
            )
            self.assertTrue(handoff["ready_for_download_planning"])
            self.assertEqual("Negative", handoff["technical_settings"]["ion_mode"])
            self.assertEqual(2, len(handoff["class_proposal"]["assignments"]))
            self.assertEqual([], handoff["blocking_reasons"])
            self.assertIn("allowlist_required", handoff["download_scope"])
            self.assertTrue(handoff["files_omitted"])
            self.assertTrue(handoff["sample_metadata_omitted"])
            self.assertTrue(Path(handoff["handoff_path"]).is_file())

    def test_handoff_uses_one_sample_row_per_primary_wiff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = str(Path(temporary) / "catalog.sqlite")
            rows = []
            files = []
            for energy in (14, 18):
                stem = f"sample_EAD{energy}"
                attributes = {"analyticalCondition / Collision energy": f"{energy} eV"}
                rows.extend(
                    [
                        {"sample_id": stem, "raw_file": f"{stem}.wiff", "values": attributes},
                        {"sample_id": stem, "raw_file": f"{stem}.wiff2", "values": attributes},
                        {
                            "sample_id": f"{stem}.timeseries",
                            "raw_file": f"{stem}.timeseries.data",
                            "values": attributes,
                        },
                    ]
                )
                for suffix in (".wiff", ".wiff.scan", ".wiff2", ".timeseries.data"):
                    files.append(
                        {
                            "name": f"{stem}{suffix}",
                            "size_bytes": 100,
                            "role": "raw",
                            "url": "https://example.org/bundle",
                            "sample_id": stem,
                        }
                    )
            project = {
                "repository": "mb_post",
                "accession": "MPST-WIFF",
                "title": "WIFF container test",
                "analysis_units": [
                    {
                        "source_subrecord_id": "ead",
                        "label": "EAD",
                        "separation": "LC-MS",
                        "chromatography": "Reversed phase",
                        "ion_mode": "Positive",
                        "acquisition_mode": "DDA",
                        "target_omics": "Lipidomics",
                        "untargeted": True,
                        "sample_metadata": rows,
                        "files": files,
                    }
                ],
            }
            study = project_to_study(project)
            with Catalog(database) as catalog:
                catalog.ingest_study(study)
            handoff = msdial_catalog_reanalysis_handoff(
                study.analysis_units[0].unit_id, database=database
            )
            unit = msdial_catalog_get_analysis_unit(
                study.analysis_units[0].unit_id, database=database
            )
            search = msdial_catalog_search(
                accessions=["MPST-WIFF"], database=database
            )
            sample_rows = json.loads(Path(handoff["sample_table_path"]).read_text(encoding="utf-8"))

            self.assertEqual(2, handoff["sample_count"])
            self.assertEqual(2, handoff["analytical_sample_count"])
            self.assertEqual(2, len(sample_rows))
            self.assertTrue(all(row["raw_file"].endswith(".wiff") for row in sample_rows))
            self.assertTrue(all(len(row["related_files"]) == 3 for row in sample_rows))
            self.assertEqual("none_recorded", handoff["publication_status"])
            self.assertEqual(2, unit["sample_count"])
            self.assertTrue(unit["samples_omitted"])
            self.assertTrue(Path(unit["sample_table_path"]).is_file())
            roles = {Path(item["path"]).suffix: item["role"] for item in unit["files"]}
            self.assertEqual("raw_alternate", roles[".wiff2"])
            self.assertTrue(
                all(
                    item["role"] == "auxiliary"
                    for item in unit["files"]
                    if item["path"].endswith(".timeseries.data")
                )
            )
            self.assertEqual(2, search["matches"][0]["sample_count"])


if __name__ == "__main__":
    unittest.main()

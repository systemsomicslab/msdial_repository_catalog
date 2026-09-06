from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from msdial_repository_catalog.mcp_server import (
    msdial_catalog_class_request,
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
                database=database,
            )
            self.assertEqual(unit_id, result["matches"][0]["unit_id"])
            request = msdial_catalog_class_request(
                unit_id, "Compare age and genotype.", database
            )
            self.assertEqual(2, len(request["samples"]))

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


if __name__ == "__main__":
    unittest.main()

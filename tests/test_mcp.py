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
                # The dangling keys CLAUDE-C05 recorded: a sidecar names its parent
                # file and an auxiliary container names a derived basename, neither
                # of which is a row in the sample table.
                ingested_ids = {
                    ".wiff": stem,
                    ".wiff.scan": f"{stem}.wiff",
                    ".wiff2": stem,
                    ".timeseries.data": f"{stem}.timeseries",
                }
                for suffix in (".wiff", ".wiff.scan", ".wiff2", ".timeseries.data"):
                    files.append(
                        {
                            "name": f"{stem}{suffix}",
                            "size_bytes": 100,
                            "role": "raw",
                            "url": "https://example.org/bundle",
                            "sample_id": ingested_ids[suffix],
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
            # One row per sample, named for the container that will be analysed. That is the
            # .wiff2 from 2026-09-21: the two encode the same acquisition, exactly one may be
            # analysed, and the analyst chose .wiff2 for every acquisition rather than only for
            # SCIEX ZT Scan DIA - which would have needed an acquisition method no repository
            # field states.
            self.assertTrue(all(row["raw_file"].endswith(".wiff2") for row in sample_rows))
            self.assertTrue(all(len(row["related_files"]) == 3 for row in sample_rows))
            self.assertEqual("none_recorded", handoff["publication_status"])
            self.assertEqual(2, unit["sample_count"])
            self.assertTrue(unit["samples_omitted"])
            self.assertTrue(Path(unit["sample_table_path"]).is_file())
            self.assertTrue(unit["files_omitted"])
            self.assertEqual([], unit["files"])
            self.assertTrue(Path(unit["file_manifest_path"]).is_file())
            file_rows = json.loads(
                Path(unit["file_manifest_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(unit["file_count"], len(file_rows))
            roles = {Path(item["path"]).suffix: item["role"] for item in file_rows}
            # The .wiff2 is the container analysed and the .wiff is its alternate, reversed from
            # the original rule on 2026-09-21. Both encode the same acquisition and exactly one
            # may be analysed, or the sample is measured twice.
            self.assertEqual("raw", roles[".wiff2"])
            self.assertEqual("raw_alternate", roles[".wiff"])
            self.assertTrue(
                all(
                    item["role"] == "auxiliary"
                    for item in file_rows
                    if item["path"].endswith(".timeseries.data")
                )
            )
            # CLAUDE-C05: every sample_id in the manifest names a row in the sample table.
            sample_ids = {row["sample_id"] for row in sample_rows}
            for item in file_rows:
                if item.get("sample_id"):
                    self.assertIn(item["sample_id"], sample_ids, item["path"])
                    self.assertTrue(item["sample_id_resolved"])
                else:
                    self.assertFalse(item["sample_id_resolved"])
            # Every sidecar and auxiliary resolves to the container that will be analysed, which
            # is what makes its sample_id nameable. Since 2026-09-21 that container is the .wiff2.
            # A .wiff.scan is the SCIEX pair-mate of the .wiff rather than of the .wiff2, but what
            # parent_file exists to answer is which analytical sample the file belongs to, and
            # that sample is represented by the container the run opens.
            primary = {item["path"] for item in file_rows if item["role"] == "raw"}
            self.assertTrue(primary)
            for item in file_rows:
                if item["role"] in {"sidecar", "auxiliary"}:
                    self.assertIn(item.get("parent_file", ""), primary, item["path"])
            self.assertEqual(2, search["matches"][0]["sample_count"])

    def test_analysis_unit_response_stays_bounded_on_a_large_file_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = str(Path(temporary) / "catalog.sqlite")
            rows = []
            files = []
            for index in range(200):
                stem = f"sample_{index:03d}"
                rows.append(
                    {"sample_id": stem, "raw_file": f"{stem}.mzML", "values": {"Group": "control"}}
                )
                files.append(
                    {
                        "name": f"FILES/a_long_repository_path_segment/{stem}.mzML",
                        "size_bytes": 10_000,
                        "role": "raw",
                        "url": "https://example.org/MPST-BIG/bundle.zip",
                        "sample_id": stem,
                        "checksum": "0" * 64,
                    }
                )
            project = {
                "repository": "mb_post",
                "accession": "MPST-BIG",
                "title": "Bounded response test",
                "analysis_units": [
                    {
                        "source_subrecord_id": "big",
                        "label": "big",
                        "separation": "LC-MS",
                        "chromatography": "Reversed phase",
                        "ion_mode": "Positive",
                        "acquisition_mode": "DDA",
                        "target_omics": "Metabolomics",
                        "untargeted": True,
                        "sample_metadata": rows,
                        "files": files,
                    }
                ],
            }
            study = project_to_study(project)
            with Catalog(database) as catalog:
                catalog.ingest_study(study)
            unit = msdial_catalog_get_analysis_unit(
                study.analysis_units[0].unit_id, database=database
            )

            self.assertEqual(200, unit["file_count"])
            self.assertEqual(200, unit["sample_count"])
            self.assertTrue(unit["files_omitted"])
            self.assertTrue(unit["samples_omitted"])
            self.assertTrue(Path(unit["file_manifest_path"]).is_file())
            self.assertTrue(Path(unit["sample_table_path"]).is_file())
            serialized = json.dumps(unit, ensure_ascii=False)
            self.assertLess(len(serialized), 20_000, len(serialized))

    def test_files_are_returned_only_when_asked_for_and_can_be_capped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = str(Path(temporary) / "catalog.sqlite")
            rows = [
                {"sample_id": f"s{index}", "raw_file": f"s{index}.mzML", "values": {"Group": "g"}}
                for index in range(5)
            ]
            files = [
                {
                    "name": f"s{index}.mzML",
                    "size_bytes": 10,
                    "role": "raw",
                    "url": "https://example.org/bundle",
                    "sample_id": f"s{index}",
                }
                for index in range(5)
            ]
            project = {
                "repository": "mb_post",
                "accession": "MPST-SMALL",
                "title": "Opt-in files",
                "analysis_units": [
                    {
                        "source_subrecord_id": "small",
                        "label": "small",
                        "separation": "LC-MS",
                        "chromatography": "Reversed phase",
                        "ion_mode": "Positive",
                        "acquisition_mode": "DDA",
                        "target_omics": "Metabolomics",
                        "untargeted": True,
                        "sample_metadata": rows,
                        "files": files,
                    }
                ],
            }
            study = project_to_study(project)
            with Catalog(database) as catalog:
                catalog.ingest_study(study)
            unit_id = study.analysis_units[0].unit_id
            full = msdial_catalog_get_analysis_unit(
                unit_id, database=database, include_files=True
            )
            capped = msdial_catalog_get_analysis_unit(
                unit_id, database=database, include_files=True, file_limit=2
            )

            self.assertEqual(5, len(full["files"]))
            self.assertFalse(full["files_truncated"])
            self.assertEqual(2, len(capped["files"]))
            self.assertTrue(capped["files_truncated"])
            self.assertEqual(5, capped["file_count"])


if __name__ == "__main__":
    unittest.main()

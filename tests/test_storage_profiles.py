from __future__ import annotations

import gzip
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from msdial_repository_catalog.normalize import project_to_study
from msdial_repository_catalog.schema import BASE_SCHEMA, SCHEMA_VERSION
from msdial_repository_catalog.storage import Catalog


def project(accession: str = "MPST-STORAGE-TEST") -> dict:
    return {
        "repository": "mb_post",
        "accession": accession,
        "title": "Compact storage test",
        "source_marker": "source metadata retained in provenance",
        "analysis_units": [{
            "source_subrecord_id": "negative-dda",
            "label": "Negative DDA",
            "separation": "LC-MS",
            "ion_mode": "Negative",
            "acquisition_mode": "DDA",
            "sample_metadata": [{
                "sample_id": "S1",
                "raw_file": "S1.raw",
                "values": {"Group": "Control", "Age": "12 weeks"},
            }],
            "files": [{"name": "S1.raw", "size_bytes": 100}],
        }],
    }


class StorageProfileTests(unittest.TestCase):
    def test_schema_one_is_migrated_additively(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "catalog.sqlite"
            legacy_schema = BASE_SCHEMA.replace(
                "    current_snapshot_id TEXT NOT NULL DEFAULT '',\n", ""
            ).replace(
                "    source_blob_hash TEXT NOT NULL DEFAULT '',\n", ""
            )
            connection = sqlite3.connect(database)
            try:
                connection.executescript(legacy_schema)
                connection.execute("INSERT INTO schema_info(version) VALUES (1)")
                connection.commit()
            finally:
                connection.close()
            with Catalog(database) as catalog:
                catalog.initialize()
                version = catalog.connection.execute(
                    "SELECT version FROM schema_info"
                ).fetchone()[0]
                study_columns = {
                    row[1] for row in catalog.connection.execute("PRAGMA table_info(study)")
                }
                snapshot_columns = {
                    row[1]
                    for row in catalog.connection.execute("PRAGMA table_info(source_snapshot)")
                }
            self.assertEqual(SCHEMA_VERSION, version)
            self.assertIn("current_snapshot_id", study_columns)
            self.assertIn("source_blob_hash", snapshot_columns)

    def test_source_payload_is_compressed_once_and_can_be_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "catalog.sqlite"
            study = project_to_study(project())
            with Catalog(database) as catalog:
                catalog.ingest_study(study)
                catalog.ingest_study(study)
                blob_count = catalog.connection.execute(
                    "SELECT COUNT(*) FROM source_blob"
                ).fetchone()[0]
                legacy = catalog.connection.execute(
                    "SELECT source_payload_json FROM study WHERE study_id = ?",
                    (study.study_id,),
                ).fetchone()[0]
                restored = catalog.source_payload(study.repository, study.accession)
                unit = catalog.get_unit(study.analysis_units[0].unit_id)
            self.assertEqual(1, blob_count)
            self.assertEqual("", legacy)
            self.assertEqual(study.source_payload, restored)
            self.assertEqual("Control", unit["samples"][0]["attributes"]["Group"])
            self.assertEqual("12 weeks", unit["samples"][0]["attributes"]["Age"])

    def test_compact_storage_migrates_legacy_payload_copies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "catalog.sqlite"
            study = project_to_study(project())
            payload_json = json.dumps(study.source_payload, ensure_ascii=False, sort_keys=True)
            with Catalog(database) as catalog:
                catalog.ingest_study(study)
                with catalog.connection:
                    catalog.connection.execute("DELETE FROM source_blob")
                    catalog.connection.execute(
                        "UPDATE study SET source_payload_json = ?", (payload_json,)
                    )
                    catalog.connection.execute(
                        "UPDATE source_snapshot SET source_blob_hash = '', source_payload_json = ?",
                        (payload_json,),
                    )
                result = catalog.compact_source_storage(vacuum=False)
                restored = catalog.source_payload(study.repository, study.accession)
            self.assertEqual(1, result["migrated_snapshots"])
            self.assertEqual(study.source_payload, restored)
            self.assertGreater(result["after"]["source_compressed_bytes"], 0)
            self.assertLess(result["after"]["legacy_json_bytes"], result["before"]["legacy_json_bytes"])

    def test_thin_repository_snapshot_excludes_source_blobs_and_other_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "catalog.sqlite"
            output = root / "mb-post-thin.sqlite.gz"
            with Catalog(database) as catalog:
                catalog.ingest_study(project_to_study(project()))
                other = project("STORAGE-OTHER")
                other["repository"] = "metabolights"
                catalog.ingest_study(project_to_study(other))
                manifest = catalog.snapshot(output, profile="thin", repository="mb_post")
            extracted = root / "thin.sqlite"
            with gzip.open(output, "rb") as source:
                extracted.write_bytes(source.read())
            connection = sqlite3.connect(extracted)
            try:
                repositories = [row[0] for row in connection.execute(
                    "SELECT DISTINCT repository FROM study"
                )]
                blob_count = connection.execute("SELECT COUNT(*) FROM source_blob").fetchone()[0]
                unit_count = connection.execute("SELECT COUNT(*) FROM analysis_unit").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(["mb_post"], repositories)
            self.assertEqual(0, blob_count)
            self.assertEqual(1, unit_count)
            self.assertEqual("thin", manifest["profile"])
            self.assertEqual("mb_post", manifest["repository"])

    def test_release_bundle_writes_repository_shards_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with Catalog(root / "catalog.sqlite") as catalog:
                catalog.ingest_study(project_to_study(project()))
                result = catalog.release_bundle(root / "release")
            self.assertEqual(["mb_post"], result["repositories"])
            self.assertEqual(1, len(result["assets"]))
            self.assertTrue(Path(result["manifest_path"]).exists())
            self.assertTrue((root / "release" / result["assets"][0]["asset"]).exists())


if __name__ == "__main__":
    unittest.main()

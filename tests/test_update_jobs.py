from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from msdial_repository_catalog.normalize import project_to_study
from msdial_repository_catalog.storage import Catalog
from msdial_repository_catalog.update_jobs import UpdateJobManager


def project(repository: str = "mb_post", accession: str = "MPST-UPDATE-TEST") -> dict:
    return {
        "repository": repository,
        "accession": accession,
        "title": "Update job test",
        "analysis_units": [{
            "source_subrecord_id": "negative-dda",
            "label": "Negative DDA",
            "separation": "LC-MS",
            "ion_mode": "Negative",
            "acquisition_mode": "DDA",
            "sample_metadata": [{"sample_id": "S1", "raw_file": "S1.raw"}],
            "files": [{"name": "S1.raw", "size_bytes": 100}],
        }],
    }


class FakeAdapter:
    def __init__(self, repository: str) -> None:
        self.name = repository

    def list_accessions(self) -> list[str]:
        return ["MPST-UPDATE-TEST"]

    def inspect_metadata(self, accession: str) -> dict:
        return project(self.name, accession)


class UpdateJobTests(unittest.TestCase):
    def test_indexed_update_refreshes_only_local_accessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "catalog.sqlite"
            with Catalog(database) as catalog:
                catalog.ingest_study(project_to_study(project()))
            manager = UpdateJobManager(database, adapter_factory=FakeAdapter)
            manager.start(["mb_post"], mode="indexed")
            first = manager.wait(timeout=5)
            self.assertEqual("completed", first["state"])
            manager.start(["mb_post"], mode="indexed")
            result = manager.wait(timeout=5)
            self.assertEqual(1, result["unchanged"])
            self.assertEqual(100.0, result["percent"])

    def test_discovery_update_populates_an_empty_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "catalog.sqlite"
            manager = UpdateJobManager(database, adapter_factory=FakeAdapter)
            manager.start(["mb_post"], mode="discover")
            result = manager.wait(timeout=5)
            self.assertEqual("completed", result["state"])
            self.assertEqual(1, result["hydrated"])
            with Catalog(database) as catalog:
                self.assertEqual(["MPST-UPDATE-TEST"], catalog.accessions("mb_post"))

    def test_second_job_is_rejected_while_running(self) -> None:
        class BlockingAdapter(FakeAdapter):
            def inspect_metadata(self, accession: str) -> dict:
                self.release.wait(timeout=5)
                return super().inspect_metadata(accession)

        BlockingAdapter.release = __import__("threading").Event()
        with tempfile.TemporaryDirectory() as temporary:
            manager = UpdateJobManager(Path(temporary) / "catalog.sqlite", adapter_factory=BlockingAdapter)
            manager.start(["mb_post"], mode="discover")
            with self.assertRaises(RuntimeError):
                manager.start(["mb_post"], mode="discover")
            manager.cancel()
            BlockingAdapter.release.set()
            result = manager.wait(timeout=5)
            self.assertEqual("cancelled", result["state"])


if __name__ == "__main__":
    unittest.main()

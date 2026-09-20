"""The download figure a person is asked to approve must be the bytes actually fetched.

The project contract names download_scope.bundle_bytes "the download approval and safety-limit
quantity". It was SUM(size_bytes) over raw_file rows sharing a download_url, and raw_file holds one
row per (unit, file) -- so an archive that several analysis units share was multiplied by the number
of units. Measured against the live index: ST004151_Rawfiles.zip is 38.0 GB and was reported as
380.1 GB across its ten units; ST002965_Rawdata.zip is 54.0 GB and was reported as 216.0 GB across
four. 5,355 download URLs are shared by more than one unit.

Two shapes share the column and only one of them wants a sum, which is why neither SUM nor MAX is
right on its own:

  archive        one path, repeated once per unit, downloaded once     -> count it once
  per-file API   many paths, one row each, all genuinely downloaded    -> add them up

Grouping by path before summing counts each distinct file once and is correct for both.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from msdial_repository_catalog.storage import Catalog


class DownloadScopeCountsEachFileOnce(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.catalog = Catalog(Path(self.directory.name) / "catalog.sqlite")
        self.catalog.initialize()
        self._seed()

    def tearDown(self) -> None:
        self.catalog.close()
        self.directory.cleanup()

    def _seed(self) -> None:
        """Write the rows directly: this is a property of the aggregation, not of ingestion."""
        connection = self.catalog.connection
        units = [f"unit-archive-{index}" for index in range(4)] + ["unit-per-file"]
        connection.executemany(
            "INSERT INTO study (study_id, repository, accession, source_hash)"
            " VALUES (?, ?, ?, ?)",
            [("study-1", "example", "STUDY", "seed")],
        )
        connection.executemany(
            "INSERT INTO analysis_unit (unit_id, study_id, source_subrecord_id, signature)"
            " VALUES (?, ?, ?, ?)",
            [(unit, "study-1", unit, "seed") for unit in units],
        )
        rows = []
        # One 40 GB archive referenced by four analysis units, as Metabolomics Workbench presents it.
        for index in range(4):
            rows.append((f"file-archive-{index}", f"unit-archive-{index}", "STUDY_Rawfiles.zip",
                         40_000_000_000, "https://example.org/studydownload/STUDY_Rawfiles.zip"))
        # One per-file endpoint serving three distinct files, as MetaboBank presents it.
        for index, size in enumerate((10_000_000, 20_000_000, 30_000_000)):
            rows.append((f"file-per-{index}", "unit-per-file", f"sample_{index}.CDF",
                         size, "https://example.org/api/download/MPSTX.0"))
        connection.executemany(
            "INSERT INTO raw_file (file_id, unit_id, path, size_bytes, download_url)"
            " VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        connection.commit()

    def test_an_archive_shared_by_several_units_is_counted_once(self) -> None:
        scope = self.catalog.download_scope(
            ["https://example.org/studydownload/STUDY_Rawfiles.zip"]
        )

        self.assertEqual(40_000_000_000, scope["bundle_bytes"],
                         "the archive is fetched once however many units cite it")
        self.assertEqual(4, scope["bundle_shared_unit_count"],
                         "and the sharing is still reported, because it is why the figure is not per-unit")

    def test_a_per_file_endpoint_still_sums_its_files(self) -> None:
        scope = self.catalog.download_scope(
            ["https://example.org/api/download/MPSTX.0"]
        )

        self.assertEqual(60_000_000, scope["bundle_bytes"],
                         "three distinct files are three downloads")

    def test_several_urls_add_up(self) -> None:
        scope = self.catalog.download_scope([
            "https://example.org/studydownload/STUDY_Rawfiles.zip",
            "https://example.org/api/download/MPSTX.0",
        ])

        self.assertEqual(40_060_000_000, scope["bundle_bytes"])
        self.assertEqual(2, len(scope["urls"]))

    def test_an_unknown_url_contributes_nothing_rather_than_guessing(self) -> None:
        scope = self.catalog.download_scope(["https://example.org/not-indexed.zip"])

        self.assertEqual(0, scope["bundle_bytes"])
        self.assertEqual([], scope["urls"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

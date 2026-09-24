"""An adapter fix reaches the records it was written for without a re-crawl.

A crawl skips a study whose source hash and parser version both match what is stored, so the
2026-09-21 full re-crawl ran for twenty-five hours and re-applied nothing. The payloads it would
have re-fetched are already stored; the local re-parse rebuilds from them. These tests hold it to
what it promises: it re-applies the sample attribution, it leaves every inferred field alone, it
says so when a payload cannot be re-parsed, and running it twice changes nothing the second time.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from msdial_repository_catalog.normalize import project_to_study
from msdial_repository_catalog.reparse import reparse_repository
from msdial_repository_catalog.storage import Catalog


def _factor(name: str) -> dict:
    return {"local_sample_id": name, "raw_data": f"{name}.mzML", "factors": "Group:A"}


def _workbench_study(accession: str, *, with_factors: bool = True) -> dict:
    """The pre-fix shape: every unit of the study carries the whole study's sample list."""
    names = ["S01_POS", "S02_POS", "S01_NEG", "S02_NEG"]
    every_sample = [{"sample_id": name, "raw_file": f"{name}.mzML", "values": {}} for name in names]
    unit = {
        "separation": "LC-MS",
        "acquisition_mode": "DDA",
        "untargeted": True,
        "target_omics": "Lipidomics",
        "sample_metadata": every_sample,
        "warnings": ["Metabolomics Workbench factors are study-level; verify each sample."],
        "files": [{"name": f"{accession}.zip", "size_bytes": 10, "role": "archive"}],
    }
    return {
        "repository": "metabolomics_workbench",
        "accession": accession,
        "title": accession,
        "repository_metadata": {"factors": [_factor(name) for name in names]} if with_factors else {},
        "analysis_units": [
            {**unit, "source_subrecord_id": "AN1", "ion_mode": "Positive"},
            {**unit, "source_subrecord_id": "AN2", "ion_mode": "Negative"},
        ],
    }


class ReparseTests(unittest.TestCase):
    def _catalog(self, root: Path, *projects: dict) -> Catalog:
        catalog = Catalog(root / "catalog.sqlite")
        for project in projects:
            catalog.ingest_study(project_to_study(project, "0.5.0"))
        return catalog

    def test_the_sample_attribution_is_re_applied_from_the_stored_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self._catalog(Path(temporary), _workbench_study("ST000001")) as catalog:
                summary = reparse_repository(catalog, "metabolomics_workbench", parser_version="0.6.0")
                payload = catalog.source_payload("metabolomics_workbench", "ST000001")

        self.assertEqual(1, summary.reparsed)
        positive, negative = payload["analysis_units"]
        self.assertEqual(["S01_POS", "S02_POS"], [item["sample_id"] for item in positive["sample_metadata"]])
        self.assertEqual(["S01_NEG", "S02_NEG"], [item["sample_id"] for item in negative["sample_metadata"]])
        self.assertFalse(any("verify each sample" in item for item in positive["warnings"]))
        self.assertTrue(any("2 of 4 belong" in item for item in positive["warnings"]), positive["warnings"])

    def test_inferred_fields_are_left_exactly_as_stored(self) -> None:
        """The detail page they were inferred from is not stored, so they are not re-derived."""
        with tempfile.TemporaryDirectory() as temporary:
            with self._catalog(Path(temporary), _workbench_study("ST000002")) as catalog:
                reparse_repository(catalog, "metabolomics_workbench", parser_version="0.6.0")
                unit = catalog.source_payload("metabolomics_workbench", "ST000002")["analysis_units"][0]

        self.assertEqual(
            ("LC-MS", "DDA", True, "Lipidomics"),
            (unit["separation"], unit["acquisition_mode"], unit["untargeted"], unit["target_omics"]),
        )

    def test_a_second_pass_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self._catalog(Path(temporary), _workbench_study("ST000003")) as catalog:
                reparse_repository(catalog, "metabolomics_workbench", parser_version="0.6.0")
                again = reparse_repository(catalog, "metabolomics_workbench", parser_version="0.6.0")

        self.assertEqual((0, 1), (again.reparsed, again.unchanged))

    def test_a_payload_without_the_factor_table_is_reported_not_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            study = _workbench_study("ST000004", with_factors=False)
            with self._catalog(Path(temporary), study) as catalog:
                summary = reparse_repository(catalog, "metabolomics_workbench", parser_version="0.6.0")
                payload = catalog.source_payload("metabolomics_workbench", "ST000004")

        self.assertEqual((0, 1), (summary.reparsed, summary.not_supported))
        self.assertEqual(4, len(payload["analysis_units"][0]["sample_metadata"]))

    def test_an_adapter_without_a_local_re_parse_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            study = {**_workbench_study("MTBLS1"), "repository": "metabolights", "accession": "MTBLS1"}
            with self._catalog(Path(temporary), study) as catalog:
                summary = reparse_repository(catalog, "metabolights", parser_version="0.6.0")

        self.assertEqual((0, 1), (summary.reparsed, summary.not_supported))


if __name__ == "__main__":
    unittest.main()

"""The selection layer reaches the agent through MCP, and the notice reaches the database.

The user's condition for letting an agent choose Class unattended was that the notification
survive: Class does not affect peak detection, alignment or annotation, so an imperfect
grouping is something a researcher corrects afterwards - provided the run says plainly that a
machine chose it and nobody checked. That only holds if the notice is stored, not printed.

These tests run the whole path an agent takes: report the selection, preview it, save it under
the same confirmation the contract has always required, and read the warnings back out of
SQLite. The abstention path is tested for the opposite property - that it writes nothing.
"""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from msdial_repository_catalog.mcp_server import (
    msdial_catalog_class_selection,
    msdial_catalog_save_class_proposal,
)
from msdial_repository_catalog.normalize import project_to_study
from msdial_repository_catalog.storage import Catalog

from test_catalog import mixed_project


def _declared_project() -> dict:
    """The same study, with the design deposited as ISA-Tab factor columns."""
    project = copy.deepcopy(mixed_project())
    for unit in project["analysis_units"]:
        for index, sample in enumerate(unit["sample_metadata"]):
            values = sample["values"]
            values["Factor Value[Treatment]"] = "vehicle" if index % 2 == 0 else "LPS"
            values["Factor Value[Batch]"] = "batch 1"
    return project


def _ingest(project: dict, database: str) -> str:
    study = project_to_study(project)
    with Catalog(database) as catalog:
        catalog.ingest_study(study)
    return study.analysis_units[0].unit_id


class TheAgentCanAskWhatWouldDefineClass(unittest.TestCase):
    def test_a_declared_factor_is_reported_with_the_groups_it_would_make(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = str(Path(temporary) / "catalog.sqlite")
            unit_id = _ingest(_declared_project(), database)

            decision = msdial_catalog_class_selection(unit_id, "Compare LPS against vehicle", database)

        self.assertEqual("declared", decision["decision"])
        self.assertEqual(["Factor Value[Treatment]"], decision["selected_fields"])
        self.assertEqual({"vehicle": 1, "LPS": 1}, decision["class_counts"])
        self.assertIn("Factor Value[Batch]", decision["design_covariates"])

    def test_the_columns_that_read_like_a_design_but_declare_nothing_are_refused(self) -> None:
        """The existing fixture deposits Genotype, Treatment and Age group as plain columns.

        Every one of them separates the two samples cleanly, and none of them states that the
        experiment varied it. `candidate_fields` ranks them; this declines to choose one.
        """
        with tempfile.TemporaryDirectory() as temporary:
            database = str(Path(temporary) / "catalog.sqlite")
            unit_id = _ingest(mixed_project(), database)

            decision = msdial_catalog_class_selection(unit_id, "Compare genotypes", database)

        self.assertEqual("abstained", decision["decision"])
        self.assertEqual("no_declared_factor", decision["reason"])
        self.assertGreater(decision["undeclared_columns"], 0)


class SavingItStillNeedsTheConfirmation(unittest.TestCase):
    def test_empty_selected_fields_previews_the_chosen_factor_rather_than_saving(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = str(Path(temporary) / "catalog.sqlite")
            unit_id = _ingest(_declared_project(), database)

            preview = msdial_catalog_save_class_proposal(
                unit_id, "Compare LPS against vehicle", [], "", "", database=database
            )

            with Catalog(database) as catalog:
                stored = catalog.connection.execute(
                    "SELECT COUNT(*) FROM class_proposal"
                ).fetchone()[0]

        self.assertTrue(preview["confirmation_required"])
        self.assertEqual(["Factor Value[Treatment]"], preview["proposal_preview"]["selected_fields"])
        self.assertEqual("declared", preview["class_selection"]["decision"])
        self.assertEqual(0, stored, "a preview saves nothing")

    def test_the_notice_survives_into_sqlite_where_a_researcher_will_find_it(self) -> None:
        """THE USER'S CONDITION. An agent-chosen grouping must announce itself later."""
        with tempfile.TemporaryDirectory() as temporary:
            database = str(Path(temporary) / "catalog.sqlite")
            unit_id = _ingest(_declared_project(), database)

            saved = msdial_catalog_save_class_proposal(
                unit_id, "Compare LPS against vehicle", [], "", "", confirmed=True, database=database
            )

            with Catalog(database) as catalog:
                stored = catalog.get_class_proposal(saved["proposal"]["proposal_id"])

        self.assertTrue(saved["saved"])
        self.assertEqual(
            "accepted", stored["status"],
            "the confirmation is what makes it accepted, and it must be readable afterwards",
        )
        self.assertEqual(["Factor Value[Treatment]"], stored["selected_fields"])
        self.assertIn("without a person reading the study", stored["warnings"][0])
        self.assertEqual("catalog-declared-factor-selection", stored["model"])
        self.assertEqual(
            {"LPS", "vehicle"}, {item["class_label"] for item in stored["assignments"]}
        )

    def test_a_preview_leaves_the_proposal_unaccepted(self) -> None:
        """The status must record the confirmation, not the act of looking at the proposal."""
        with tempfile.TemporaryDirectory() as temporary:
            database = str(Path(temporary) / "catalog.sqlite")
            unit_id = _ingest(_declared_project(), database)

            preview = msdial_catalog_save_class_proposal(
                unit_id, "Compare LPS against vehicle", [], "", "", database=database
            )

        self.assertTrue(preview["confirmation_required"])
        self.assertNotIn("proposal", preview, "nothing was saved, so nothing was accepted")

    def test_an_abstention_saves_nothing_and_returns_the_reason(self) -> None:
        """Confirming does not turn an abstention into a guess."""
        with tempfile.TemporaryDirectory() as temporary:
            database = str(Path(temporary) / "catalog.sqlite")
            unit_id = _ingest(mixed_project(), database)

            result = msdial_catalog_save_class_proposal(
                unit_id, "Compare genotypes", [], "", "", confirmed=True, database=database
            )

            with Catalog(database) as catalog:
                stored = catalog.connection.execute(
                    "SELECT COUNT(*) FROM class_proposal"
                ).fetchone()[0]

        self.assertFalse(result["saved"])
        self.assertEqual("no_declared_factor", result["class_selection"]["reason"])
        self.assertIn("declared no experimental factor", result["message"])
        self.assertEqual(0, stored)

    def test_naming_the_fields_still_uses_them_and_nothing_else_changed(self) -> None:
        """The path a person takes is untouched: their fields, their rationale, their model."""
        with tempfile.TemporaryDirectory() as temporary:
            database = str(Path(temporary) / "catalog.sqlite")
            unit_id = _ingest(_declared_project(), database)

            saved = msdial_catalog_save_class_proposal(
                unit_id,
                "Compare genotypes",
                ["Genotype"],
                "",
                "A person read the study and chose Genotype.",
                model="reviewer",
                confirmed=True,
                database=database,
            )

        self.assertEqual(["Genotype"], saved["proposal"]["selected_fields"])
        self.assertEqual("reviewer", saved["proposal"]["model"])
        self.assertEqual("A person read the study and chose Genotype.", saved["proposal"]["rationale"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""A campaign approval stands in for the confirmation that saves a Class decision, and says so.

Boundary 3 is a person reading a grouping, or an abstention, and agreeing to it. A campaign of
several hundred units replaces the per-unit question with one approval of one manifest digest. These
tests hold the replacement to three properties: the approval is recorded beside the proposal it
accepted, so an audit reads which approval it was; an approval that does not name itself -- no
approval id, no digest -- is refused rather than taken for a yes; and without one, nothing about
the conversational path changed.
"""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from msdial_repository_catalog.class_selection import automatic_class_proposal
from msdial_repository_catalog.mcp_server import (
    msdial_catalog_reanalysis_handoff,
    msdial_catalog_save_class_proposal,
)
from msdial_repository_catalog.normalize import project_to_study
from msdial_repository_catalog.ratification import RatificationError
from msdial_repository_catalog.storage import Catalog

from test_catalog import mixed_project

DIGEST = "sha256:" + "0123456789abcdef" * 4
APPROVAL = {"approval_id": "approval-2026-09-30-declared", "manifest_digest": DIGEST}


def declared_project() -> dict:
    project = copy.deepcopy(mixed_project())
    for unit in project["analysis_units"]:
        for index, sample in enumerate(unit["sample_metadata"]):
            sample["values"]["Factor Value[Treatment]"] = "vehicle" if index % 2 == 0 else "LPS"
    return project


def ingest(project: dict, database: str) -> str:
    study = project_to_study(project)
    with Catalog(database) as catalog:
        catalog.ingest_study(study)
    return study.analysis_units[0].unit_id


def stored_count(database: str) -> int:
    with Catalog(database) as catalog:
        return catalog.connection.execute("SELECT COUNT(*) FROM class_proposal").fetchone()[0]


class ARatifiedSaveNeedsNoConfirmation(unittest.TestCase):
    def test_a_proposal_round_trips_with_its_ratification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = str(Path(temporary) / "catalog.sqlite")
            unit_id = ingest(declared_project(), database)
            ratification = {**APPROVAL, "authorization_sha256": "f" * 64, "campaign_id": "declared-pool"}

            saved = msdial_catalog_save_class_proposal(
                unit_id, "Compare LPS against vehicle", [], "", "", ratification=ratification,
                database=database,
            )
            with Catalog(database) as catalog:
                stored = catalog.get_class_proposal(saved["proposal"]["proposal_id"])

        self.assertTrue(saved["saved"], saved)
        self.assertNotIn("confirmation_required", saved, "the campaign approval is the confirmation")
        record = saved["ratification"]
        self.assertEqual(
            ("campaign_approval", 3, APPROVAL["approval_id"], DIGEST, "f" * 64, "declared-pool", False),
            (record["kind"], record["boundary"], record["approval_id"], record["manifest_digest"],
             record["authorization_sha256"], record["campaign_id"], record["abstention"]),
        )
        self.assertEqual(saved["proposal"]["proposal_id"], record["proposal_id"])
        self.assertEqual("accepted", stored["status"])
        self.assertEqual(APPROVAL["approval_id"], stored["ratified_by"])
        self.assertEqual(record, stored["ratification"])
        self.assertEqual(["Factor Value[Treatment]"], stored["selected_fields"])

    def test_an_abstention_is_ratified_the_same_way(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = str(Path(temporary) / "catalog.sqlite")
            unit_id = ingest(mixed_project(), database)

            saved = msdial_catalog_save_class_proposal(
                unit_id, "Compare genotypes", [], "", "", abstain=True, ratification=APPROVAL,
                database=database,
            )
            with Catalog(database) as catalog:
                stored = catalog.get_class_proposal(saved["proposal"]["proposal_id"])

        self.assertTrue(saved["saved"], saved)
        self.assertTrue(saved["abstention"])
        self.assertTrue(saved["ratification"]["abstention"])
        self.assertEqual("abstention", stored["contrast_definition"]["kind"])
        self.assertEqual({"All"}, {item["class_label"] for item in stored["assignments"]})
        self.assertEqual(APPROVAL["approval_id"], stored["ratified_by"])
        self.assertEqual(saved["ratification"], stored["ratification"])

    def test_ratifying_does_not_turn_an_abstention_into_a_guess(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = str(Path(temporary) / "catalog.sqlite")
            unit_id = ingest(mixed_project(), database)

            result = msdial_catalog_save_class_proposal(
                unit_id, "Compare genotypes", [], "", "", ratification=APPROVAL, database=database
            )

            self.assertFalse(result["saved"])
            self.assertIn("abstain=True", result["next"])
            self.assertEqual(0, stored_count(database))

    def test_the_proposal_the_campaign_approved_is_the_only_one_it_ratifies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = str(Path(temporary) / "catalog.sqlite")
            unit_id = ingest(declared_project(), database)
            with Catalog(database) as catalog:
                expected, _ = automatic_class_proposal(catalog.get_unit(unit_id), "Compare LPS against vehicle")

            drifted = msdial_catalog_save_class_proposal(
                unit_id, "Compare LPS against vehicle", [], "", "",
                ratification={**APPROVAL, "proposal_id": "0" * 20}, database=database,
            )
            self.assertEqual(0, stored_count(database))
            pinned = msdial_catalog_save_class_proposal(
                unit_id, "Compare LPS against vehicle", [], "", "",
                ratification={**APPROVAL, "proposal_id": expected.proposal_id}, database=database,
            )

        self.assertFalse(drifted["saved"])
        self.assertEqual(["proposal_mismatch"], drifted["ratification_refused"])
        self.assertTrue(pinned["saved"])
        self.assertEqual(expected.proposal_id, pinned["ratification"]["proposal_id"])


class ARatificationThatDoesNotNameItselfIsRefused(unittest.TestCase):
    def refused(self, ratification: object, *, confirmed: bool = False) -> dict:
        with tempfile.TemporaryDirectory() as temporary:
            database = str(Path(temporary) / "catalog.sqlite")
            unit_id = ingest(declared_project(), database)
            result = msdial_catalog_save_class_proposal(
                unit_id, "Compare LPS against vehicle", [], "", "", confirmed=confirmed,
                ratification=ratification, database=database,
            )
            self.assertEqual(0, stored_count(database), "a refused ratification saves nothing")
        self.assertFalse(result["saved"])
        return result

    def test_no_approval_id(self) -> None:
        result = self.refused({"manifest_digest": DIGEST})
        self.assertEqual(["approval_id"], result["ratification_refused"])

    def test_no_digest(self) -> None:
        result = self.refused({"approval_id": "approval-1"})
        self.assertEqual(["manifest_digest"], result["ratification_refused"])

    def test_an_empty_ratification(self) -> None:
        result = self.refused({})
        self.assertEqual(["approval_id", "manifest_digest"], result["ratification_refused"])

    def test_a_digest_in_a_form_interactive_would_refuse(self) -> None:
        for digest in (DIGEST.upper(), DIGEST.removeprefix("sha256:"), "sha256:abc"):
            with self.subTest(digest=digest):
                result = self.refused({"approval_id": "approval-1", "manifest_digest": digest})
                self.assertEqual(["manifest_digest"], result["ratification_refused"])

    def test_a_malformed_authorization_sha256(self) -> None:
        result = self.refused({**APPROVAL, "authorization_sha256": "not-a-digest"})
        self.assertEqual(["authorization_sha256"], result["ratification_refused"])

    def test_keys_that_are_not_an_approval(self) -> None:
        result = self.refused({**APPROVAL, "library_path": "D:/private/vs21.msp"})
        self.assertEqual(["unknown_keys"], result["ratification_refused"])

    def test_confirmed_true_does_not_rescue_a_bad_ratification(self) -> None:
        """An approval that was offered and does not hold is a refusal, never ignored."""
        result = self.refused({"approval_id": "approval-1"}, confirmed=True)
        self.assertEqual(["manifest_digest"], result["ratification_refused"])


class UnratifiedBehaviourIsUnchanged(unittest.TestCase):
    def test_without_a_ratification_a_save_still_needs_the_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = str(Path(temporary) / "catalog.sqlite")
            unit_id = ingest(declared_project(), database)

            preview = msdial_catalog_save_class_proposal(
                unit_id, "Compare LPS against vehicle", [], "", "", database=database
            )
            self.assertEqual(0, stored_count(database))
            saved = msdial_catalog_save_class_proposal(
                unit_id, "Compare LPS against vehicle", [], "", "", confirmed=True, database=database
            )
            with Catalog(database) as catalog:
                stored = catalog.get_class_proposal(saved["proposal"]["proposal_id"])

        self.assertTrue(preview["confirmation_required"])
        self.assertTrue(saved["saved"])
        self.assertNotIn("ratification", saved)
        self.assertEqual("", stored["ratified_by"])
        self.assertIsNone(stored["ratification"])

    def test_a_repeated_confirmation_keeps_the_ratification_and_a_demotion_clears_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = str(Path(temporary) / "catalog.sqlite")
            unit_id = ingest(declared_project(), database)
            with Catalog(database) as catalog:
                proposal, _ = automatic_class_proposal(catalog.get_unit(unit_id), "Compare LPS against vehicle")
                proposal.status = "accepted"
                catalog.save_class_proposal(proposal, ratification=APPROVAL)
                catalog.save_class_proposal(proposal)
                kept = catalog.get_class_proposal(proposal.proposal_id)
                proposal.status = "proposed"
                catalog.save_class_proposal(proposal)
                cleared = catalog.get_class_proposal(proposal.proposal_id)

        self.assertEqual(APPROVAL["approval_id"], kept["ratified_by"])
        self.assertEqual(DIGEST, kept["ratification"]["manifest_digest"])
        self.assertEqual(("proposed", "", None), (cleared["status"], cleared["ratified_by"], cleared["ratification"]))

    def test_the_storage_layer_refuses_a_ratified_proposal_that_is_not_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = str(Path(temporary) / "catalog.sqlite")
            unit_id = ingest(declared_project(), database)
            with Catalog(database) as catalog:
                proposal, _ = automatic_class_proposal(catalog.get_unit(unit_id), "Compare LPS against vehicle")
                with self.assertRaises(ValueError):
                    catalog.save_class_proposal(proposal, ratification=APPROVAL)
                with self.assertRaises(RatificationError):
                    proposal.status = "accepted"
                    catalog.save_class_proposal(proposal, ratification={"approval_id": "approval-1"})
            self.assertEqual(0, stored_count(database))

    def test_the_handoff_carries_the_ratification_to_interactive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = str(Path(temporary) / "catalog.sqlite")
            unit_id = ingest(declared_project(), database)
            saved = msdial_catalog_save_class_proposal(
                unit_id, "Compare LPS against vehicle", [], "", "", ratification=APPROVAL, database=database
            )
            handoff = msdial_catalog_reanalysis_handoff(
                unit_id, saved["proposal"]["proposal_id"], database=database
            )

        self.assertTrue(handoff["ready_for_download_planning"], handoff["blocking_reasons"])
        self.assertEqual(APPROVAL["approval_id"], handoff["class_proposal"]["ratified_by"])
        self.assertEqual(DIGEST, handoff["class_proposal"]["ratification"]["manifest_digest"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

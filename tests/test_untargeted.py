"""A NIST Standard Reference Material is not selected reaction monitoring (2026-10-28).

MB-POST MPST000004, "Annotation of various metabolites in SRM 1950 human serum", is HILIC/AEX
HRMS/MS DDA on a Q Exactive HF in both polarities. It was stored untargeted = 0 because the targeted
words included "srm", which matched the reference material in its title, and the campaign pools
exclude a unit stored untargeted = 0. Twelve more studies in the catalog name an SRM number in their
title or description (MetaboLights MTBLS710, MTBLS1375, MTBLS1381, MTBLS1382, MTBLS1876 and
MTBLS1919; Workbench ST001797, ST001798, ST001799 and ST003514; MB-POST MPST000015; MetaboBank
MTBKS222).

These tests hold infer_untargeted to reading "SRM <number>" as the material, to still reading SRM
and MRM acquisition as targeted, and to the two other words that fired on things that are not
targeted: "targeted" inside "non-targeted", and "sim" in an identifier. The fixtures are synthetic
and shaped on the studies named; nothing here reads a repository or the catalog database.
"""

from __future__ import annotations

import unittest
from typing import Any

from msdial_repository_catalog.adapters.common import infer_untargeted
from msdial_repository_catalog.adapters.mbpost import MbPostAdapter

from test_adapters import FakeClient


class ReferenceMaterialIsNotSrmTests(unittest.TestCase):
    def test_mpst000004_title_is_not_targeted(self) -> None:
        title = "Annotation of various metabolites in SRM 1950 human serum"
        self.assertIsNone(infer_untargeted(title))
        self.assertIs(True, infer_untargeted(title, "untargeted HILIC DDA"))

    def test_every_written_form_of_a_reference_material(self) -> None:
        for text in (
            "SRM 1950",
            "SRM1950",
            "SRM-1957",
            "SRM 2378",
            "NIST SRM 1950 plasma",
            "NIST-SRM-1950",
            "nist srm1950",
            "SRM® 1950",
            "SRM 1849a infant formula",
            "SRM 909c human serum",
            "QC_SRM1950_pos_01.raw",
            "NIST SRM 1950 MS/MS spectra",
        ):
            with self.subTest(text=text):
                self.assertIsNone(infer_untargeted(text))
                self.assertIs(True, infer_untargeted("untargeted lipidomics", text))

    def test_selected_reaction_monitoring_is_still_targeted(self) -> None:
        for text in (
            "SRM transitions were optimised for each analyte",
            "acquired in SRM mode",
            "scheduled SRM on a triple quadrupole",
            "SRM 100 ms dwell time",
            "SRM 50 transitions",
            "MRM",
            "scheduled_MRM_pos",
        ):
            with self.subTest(text=text):
                self.assertIs(False, infer_untargeted(text))

    def test_a_reference_material_does_not_hide_real_srm(self) -> None:
        self.assertIs(False, infer_untargeted("NIST SRM 1950 was quantified by scheduled SRM"))
        self.assertIsNone(infer_untargeted("untargeted profiling and SRM confirmation of SRM 1950"))


class OtherTargetedWordsTests(unittest.TestCase):
    def test_non_targeted_is_untargeted(self) -> None:
        for text in ("non-targeted metabolomics", "non targeted metabolomics", "nontargeted metabolomics"):
            with self.subTest(text=text):
                self.assertIs(True, infer_untargeted(text))

    def test_targeted_is_still_targeted(self) -> None:
        self.assertIs(False, infer_untargeted("targeted bile acid panel"))
        self.assertIsNone(infer_untargeted("untargeted and targeted analysis"))

    def test_selected_ion_monitoring_is_still_targeted(self) -> None:
        for text in (
            "SIM mode",
            "selected ion monitoring (SIM)",
            "GC_SIM_mass_spectrometry",
            "20210623_SIM_SCFA_SH_1022.cdf",
        ):
            with self.subTest(text=text):
                self.assertIs(False, infer_untargeted(text))

    def test_sim_does_not_fire_on_common_words_or_identifiers(self) -> None:
        for text in (
            "similar to simulated data, simple and simultaneous",
            "simvastatin treated mice",
            "MS/MS similarity search",
            "line: SIM-006 | sex: females",
            "line:SIM-286",
        ):
            with self.subTest(text=text):
                self.assertIsNone(infer_untargeted(text))


class MbPostReferenceMaterialStudyTests(unittest.TestCase):
    def test_mpst000004_units_are_not_stored_targeted(self) -> None:
        project = {
            "location": "MPST000004.1",
            "title": "Annotation of various metabolites in SRM 1950 human serum",
            "modifiedAt": "2026-01-01",
        }
        listing = {
            "list": [
                {"id": "1", "name": "SRM1950_HILIC_pos.raw", "size": 10, "type": "raw"},
                {"id": "2", "name": "SRM1950_HILIC_neg.raw", "size": 11, "type": "raw"},
            ]
        }

        def detail(polarity: str) -> dict[str, Any]:
            return {"presets": [{"category": "analyticalCondition", "presets": [
                {"key": "presetName", "value": f"HILIC {polarity}"},
                {"key": "methodType", "value": "LC-MS"},
                {"key": "chromatographyType", "value": "HILIC"},
                {"key": "polarity", "value": polarity},
                {"key": "instrument", "value": "LC, Nexera X2; MS, Q Exactive HF"},
                {"key": "instrumentMode", "value": "HRMS/MS DDA"},
            ]}]}

        client = FakeClient({
            "files/1": detail("Positive"),
            "files/2": detail("Negative"),
            "files?": listing,
            "/api/projects/MPST000004": project,
        })
        payload = MbPostAdapter(client).inspect_metadata("MPST000004")
        self.assertEqual(2, len(payload["analysis_units"]))
        for unit in payload["analysis_units"]:
            self.assertEqual("DDA", unit["acquisition_mode"])
            self.assertIsNot(False, unit["untargeted"])


if __name__ == "__main__":
    unittest.main()

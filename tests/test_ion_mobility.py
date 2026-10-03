"""Ion mobility is read from a unit's own evidence (decided 2026-10-03, option A).

MetaboBank MTBKS217 is a Waters Xevo G2 QTOF unit. It was stored with ion mobility Enabled because
the MS-DIAL 4 lipidome-atlas abstract it shares with MTBKS218, MTBKS219, MTBKS220, MTBKS221 and
MTBKS222 says the atlas "included ion mobility tandem mass spectrometry", and the campaign plan
excluded it as LC-IM-MS on that sentence. MTBKS219 and MTBKS220 really are timsTOF units, their rows
name the instrument, but they list Bruker BAF folders (timsOFF) beside TDF folders (PASEF), and only
the TDF part is ion mobility; Interactive's per-file check and split exclude it.

These tests hold the projection the handoff and the campaign read (ion_mobility_evidence) to the
four states. The fixtures are synthetic and shaped on the units named in each test; nothing here
reads a repository or the catalog database.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from msdial_repository_catalog.adapters.common import (
    ION_MOBILITY_STUDY_TEXT_CODE,
    declared_ion_mobility,
    study_mentions_ion_mobility,
)
from msdial_repository_catalog.ion_mobility import ion_mobility_evidence
from msdial_repository_catalog.mcp_server import msdial_catalog_reanalysis_handoff
from msdial_repository_catalog.normalize import project_to_study
from msdial_repository_catalog.storage import Catalog

# The sentence MTBKS217..MTBKS222 share, from the MS-DIAL 4 lipidome-atlas abstract.
ATLAS_ABSTRACT = (
    "We present a lipidome atlas in MS-DIAL 4 that covers mass spectral fragmentations of lipids "
    "across 117 lipid subclasses and included ion mobility tandem mass spectrometry."
)
XEVO = "Xevo G2 QTOF MS (Waters, Milford, MA, USA)"
TIMSTOF = (
    "hybrid trapped ion mobility-quadrupole time-of-flight mass spectrometer "
    "(timsTOF Pro, Bruker Daltonics, Bremen, Germany)"
)


class StudyTextTests(unittest.TestCase):
    def test_a_lighting_supplier_is_no_study_mention(self) -> None:
        """MTBKS22's LED panels are from CCS Inc.; the unit-level words count CCS, a study never."""
        self.assertFalse(study_mentions_ion_mobility("LEDs (ISL-305X302, CCS Co., Kyoto, Japan)"))
        self.assertTrue(study_mentions_ion_mobility(ATLAS_ABSTRACT))


# ---- the projection ----------------------------------------------------------------------------


def waters_folder(folder: str, drift: bool = False) -> list[str]:
    names = ["_FUNC001.DAT", "_FUNC001.IDX", "_HEADER.TXT", "_FUNC002.DAT"]
    if drift:
        names.append("_FUNC002.CDT")
    return [f"{folder}/{name}" for name in names]


def bruker_folder(folder: str, binary: str) -> list[str]:
    name = folder.rsplit("/", 1)[-1]
    return [f"{folder}/{name}", f"{folder}/5512.m/lock.file", f"{folder}/{binary}"]


def unit_view(
    rows: list[tuple[str, str, dict[str, str]]],
    paths: list[str],
    *,
    instrument: str = "",
    stored: str = "Unknown",
    description: str = "",
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "unit_id": "fixture",
        "instrument": instrument,
        "ion_mobility": stored,
        "title": "A lipidome atlas",
        "description": description,
        "warnings": list(warnings or []),
        "samples": [
            {"sample_id": sample_id, "raw_file": raw_file, "attributes": dict(attributes)}
            for sample_id, raw_file, attributes in rows
        ],
        "files": [{"path": path, "role": "raw", "size_bytes": 10, "sample_id": ""} for path in paths],
    }


def mtbks217_view() -> dict[str, Any]:
    rows, paths = [], []
    for index in range(3):
        folder = f"raw/190827_0{index}pp.raw"
        rows.append((f"Plant{index}", folder + "/", {"Parameter Value[Instrument]": XEVO}))
        paths += waters_folder(folder)
    return unit_view(
        rows, paths, instrument="Waters Acquity UPLC system", stored="Enabled", description=ATLAS_ABSTRACT
    )


def mtbks220_view(baf: int = 2, tdf: int = 3) -> dict[str, Any]:
    rows, paths = [], []
    for index in range(baf + tdf):
        mode, binary, method = (
            ("timsOFF", "analysis.baf", "DDA") if index < baf else ("timsON", "analysis.tdf", "PASEF")
        )
        folder = f"raw/AG_{index}_{mode}_neg_{5000 + index}.d"
        rows.append(
            (
                f"AG_{index}",
                folder + "/",
                {
                    "Parameter Value[Chromatography instrument]": "Bruker Elute UHPLC system",
                    "Parameter Value[Instrument]": TIMSTOF,
                    "Parameter Value[Data acquisition method]": method,
                },
            )
        )
        paths += bruker_folder(folder, binary)
    return unit_view(
        rows, paths, instrument="Bruker Elute UHPLC system", stored="Enabled", description=ATLAS_ABSTRACT
    )


class EvidenceTests(unittest.TestCase):
    def test_mtbks217_a_study_mention_is_unknown_and_says_so(self) -> None:
        evidence = ion_mobility_evidence(mtbks217_view())

        self.assertEqual("unknown", evidence["state"])
        self.assertEqual("study_text", evidence["source"])
        self.assertEqual("Unknown", evidence["technical_setting"])
        self.assertEqual("Enabled", evidence["stored_ion_mobility"])
        self.assertEqual({"waters_raw": 3}, evidence["container_formats"])
        self.assertIn(XEVO, evidence["instruments"])
        self.assertEqual([], evidence["ion_mobility_instruments"])
        self.assertTrue(any(item.startswith(ION_MOBILITY_STUDY_TEXT_CODE) for item in evidence["warnings"]))

    def test_mtbks220_baf_beside_tdf_is_mixed(self) -> None:
        evidence = ion_mobility_evidence(mtbks220_view(baf=24, tdf=29))

        self.assertEqual("mixed", evidence["state"])
        self.assertEqual("row_instrument", evidence["source"])
        self.assertEqual(["row_instrument", "assay_parameter", "container_format"], evidence["sources"])
        self.assertEqual({"bruker_baf": 24, "bruker_tdf": 29}, evidence["container_formats"])
        self.assertEqual(29, evidence["ion_mobility_container_count"])
        self.assertEqual(24, evidence["non_ion_mobility_container_count"])
        self.assertEqual([TIMSTOF], evidence["ion_mobility_instruments"])
        self.assertEqual("Mixed", evidence["technical_setting"])
        self.assertEqual([], evidence["warnings"])

    def test_tdf_folders_alone_are_enabled_by_their_format(self) -> None:
        paths = bruker_folder("raw/a.d", "analysis.tdf") + bruker_folder("raw/b.d", "analysis.tdf")
        evidence = ion_mobility_evidence(
            unit_view([("a", "raw/a.d/", {}), ("b", "raw/b.d/", {})], paths)
        )

        self.assertEqual(("enabled", "container_format"), (evidence["state"], evidence["source"]))

    def test_baf_folders_alone_hold_no_mobility(self) -> None:
        paths = bruker_folder("raw/a.d", "analysis.baf")
        evidence = ion_mobility_evidence(unit_view([("a", "raw/a.d/", {})], paths))

        self.assertEqual(("none", "container_format"), (evidence["state"], evidence["source"]))
        self.assertEqual("Disabled", evidence["technical_setting"])

    def test_a_waters_folder_with_drift_files_is_ion_mobility(self) -> None:
        """Interactive reads _FUNCnnn.CDT on disk as waters_raw_im; a listing that names it says so."""
        paths = waters_folder("raw/a.raw", drift=True) + waters_folder("raw/b.raw")
        evidence = ion_mobility_evidence(
            unit_view([("a", "raw/a.raw/", {}), ("b", "raw/b.raw/", {})], paths)
        )

        self.assertEqual(("enabled", "container_format"), (evidence["state"], evidence["source"]))
        self.assertEqual({"waters_raw": 1, "waters_raw_im": 1}, evidence["container_formats"])

    def test_an_explicit_no_is_none(self) -> None:
        """MPST000007: every file's analytical condition says Ion mobility: No."""
        rows = [
            (f"s{index}", f"s{index}.lcd", {"analyticalCondition / Ion mobility": "No"}) for index in range(3)
        ]
        evidence = ion_mobility_evidence(
            unit_view(rows, [f"s{index}.lcd" for index in range(3)], instrument="Shimadzu, LCMS-9030", stored="Disabled")
        )

        self.assertEqual(("none", "assay_parameter"), (evidence["state"], evidence["source"]))

    def test_the_unit_instrument_column_alone_is_enabled(self) -> None:
        """A Workbench unit: the analysis names the instrument, the factor rows say nothing."""
        evidence = ion_mobility_evidence(
            unit_view([("s1", "s1.d", {}), ("s2", "s2.d", {})], ["s1.d", "s2.d"], instrument="Agilent 6560 Ion Mobility")
        )

        self.assertEqual(("enabled", "row_instrument"), (evidence["state"], evidence["source"]))

    def test_an_instrument_on_only_some_rows_is_mixed(self) -> None:
        rows = [
            ("s1", "s1.d", {"Parameter Value[Instrument]": "Agilent 6560 Ion Mobility Q-TOF"}),
            ("s2", "s2.d", {"Parameter Value[Instrument]": "Agilent 6545 Q-TOF"}),
        ]
        evidence = ion_mobility_evidence(unit_view(rows, ["s1.d", "s2.d"]))

        self.assertEqual("mixed", evidence["state"])
        self.assertEqual(1, evidence["rows_with_ion_mobility_evidence"])

    def test_nothing_said_is_unknown_with_no_source(self) -> None:
        evidence = ion_mobility_evidence(unit_view([("s1", "s1.mzML", {})], ["s1.mzML"]))

        self.assertEqual(("unknown", None), (evidence["state"], evidence["source"]))
        self.assertEqual([], evidence["sources"])
        self.assertEqual([], evidence["warnings"])

    def test_a_stored_enabled_nothing_supports_is_unknown_and_says_so(self) -> None:
        """MTBKS22, GC-MS: stored Enabled because its study text names "CCS Co.", an LED supplier."""
        view = unit_view(
            [("s1", "s1.cdf", {"Parameter Value[Instrument]": "Pegasus IV TOF"})], ["s1.cdf"], stored="Enabled"
        )
        evidence = ion_mobility_evidence(view)

        self.assertEqual(("unknown", "study_text"), (evidence["state"], evidence["source"]))
        self.assertFalse(evidence["study_text_mentions_ion_mobility"])
        self.assertTrue(evidence["warnings"][0].startswith("ion_mobility_stored_without_unit_evidence"))

    def test_the_adapter_warning_is_read_as_a_study_mention(self) -> None:
        """A study whose mention sits in its protocol properties, which the unit view does not carry."""
        view = unit_view(
            [("s1", "s1.mzML", {})],
            ["s1.mzML"],
            warnings=[f"{ION_MOBILITY_STUDY_TEXT_CODE}: the study's text mentions ion mobility"],
        )
        evidence = ion_mobility_evidence(view)

        self.assertEqual(("unknown", "study_text"), (evidence["state"], evidence["source"]))
        self.assertTrue(evidence["study_text_mentions_ion_mobility"])

    def test_the_projection_of_the_view_is_the_projection(self) -> None:
        from msdial_repository_catalog.class_proposal import normalize_analysis_unit

        raw = mtbks220_view()
        view = normalize_analysis_unit(raw)

        self.assertEqual(ion_mobility_evidence(raw), ion_mobility_evidence(view))


class NegationTests(unittest.TestCase):
    """A field that says ion mobility was NOT used is off, though it names the technique.

    Any value naming a technique was read as on, so "No ion mobility" or "TIMS off" in a field about
    mobility, or "DDA without ion mobility" in an acquisition field, made the unit enabled from
    assay_parameter, and the campaign plan excluded it on an explicit OFF.
    """

    OFF = (
        "No",
        "None",
        "No ion mobility",
        "No ion mobility separation",
        "Ion mobility not used",
        "Ion mobility was not used",
        "Ion mobility wasn't used",
        "without ion mobility",
        "Ion mobility: disabled",
        "IMS: none",
        "TIMS off",
        "timsOFF",
    )
    ON = (
        "Yes",
        "TIMS",
        "TIMS on",
        "timsON",
        "trapped ion mobility spectrometry (TIMS)",
        "Drift tube ion mobility",
        "TWIMS",
        "PASEF",
        "Ion mobility, nominal resolution",
    )

    def test_a_mobility_field_that_says_off_is_disabled(self) -> None:
        for value in self.OFF:
            with self.subTest(value=value):
                self.assertEqual("Disabled", declared_ion_mobility(value))

    def test_a_mobility_field_that_names_a_technique_is_still_enabled(self) -> None:
        for value in self.ON:
            with self.subTest(value=value):
                self.assertEqual("Enabled", declared_ion_mobility(value))

    def test_rows_saying_no_ion_mobility_are_none(self) -> None:
        rows = [
            (f"s{index}", f"s{index}.mzML", {"Parameter Value[Ion mobility]": "No ion mobility separation"})
            for index in range(2)
        ]
        evidence = ion_mobility_evidence(unit_view(rows, ["s0.mzML", "s1.mzML"]))

        self.assertEqual(("none", "assay_parameter"), (evidence["state"], evidence["source"]))
        self.assertEqual(2, evidence["rows_saying_ion_mobility_off"])
        self.assertEqual(0, evidence["rows_with_ion_mobility_evidence"])

    def test_an_acquisition_without_ion_mobility_is_off(self) -> None:
        rows = [("s1", "s1.mzML", {"Parameter Value[Data acquisition method]": "DDA without ion mobility"})]
        evidence = ion_mobility_evidence(unit_view(rows, ["s1.mzML"]))

        self.assertEqual(("none", "assay_parameter"), (evidence["state"], evidence["source"]))

    def test_an_instrument_field_saying_tims_off_names_no_mobility_instrument(self) -> None:
        """The instrument is a timsTOF; the field says TIMS was off, so the BAF folder decides."""
        rows = [("s1", "raw/s1.d/", {"Parameter Value[Instrument]": "timsTOF Pro, TIMS off"})]
        evidence = ion_mobility_evidence(unit_view(rows, bruker_folder("raw/s1.d", "analysis.baf")))

        self.assertEqual(("none", "container_format"), (evidence["state"], evidence["source"]))
        self.assertEqual([], evidence["ion_mobility_instruments"])


class CatalogTests(unittest.TestCase):
    """The stored column is left as the crawl wrote it; get_unit and the handoff carry the projection."""

    def ingest(self, temporary: str, view: dict[str, Any], accession: str) -> tuple[str, str]:
        database = str(Path(temporary) / "catalog.sqlite")
        unit = {
            "source_subrecord_id": "neg",
            "label": "LC-MS / Reversed phase / Negative / DIA",
            "separation": "LC-MS",
            "chromatography": "Reversed phase",
            "ion_mode": "Negative",
            "acquisition_mode": "DIA",
            "ion_mobility": view["ion_mobility"],
            "instrument": view["instrument"],
            "target_omics": "Lipidomics",
            "untargeted": True,
            "sample_metadata": [
                {"sample_id": row["sample_id"], "raw_file": row["raw_file"], "values": row["attributes"]}
                for row in view["samples"]
            ],
            "files": [
                {"name": item["path"], "size_bytes": 10, "role": "raw", "url": f"https://example/{item['path']}"}
                for item in view["files"]
            ],
        }
        study = project_to_study(
            {
                "repository": "metabobank",
                "accession": accession,
                "title": "A lipidome atlas in MS-DIAL 4",
                "description": ATLAS_ABSTRACT,
                "analysis_units": [unit],
            }
        )
        with Catalog(database) as catalog:
            catalog.ingest_study(study)
        return database, study.analysis_units[0].unit_id

    def test_mtbks217_is_stored_enabled_and_handed_off_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, unit_id = self.ingest(temporary, mtbks217_view(), "MTBKS217")
            with Catalog(database) as catalog:
                unit = catalog.get_unit(unit_id)
                evidence = catalog.ion_mobility_evidence(unit_id)
            handoff = msdial_catalog_reanalysis_handoff(unit_id, database=database)
            written = json.loads(Path(handoff["handoff_path"]).read_text(encoding="utf-8"))

            self.assertEqual("Enabled", unit["ion_mobility"], "the stored column is not rewritten")
            self.assertEqual("unknown", unit["ion_mobility_evidence"]["state"])
            self.assertEqual(unit["ion_mobility_evidence"], evidence)
            self.assertEqual("Unknown", written["technical_settings"]["ion_mobility"])
            self.assertEqual("unknown", written["ion_mobility_evidence"]["state"])
            self.assertEqual("study_text", written["ion_mobility_evidence"]["source"])
            self.assertTrue(any(item.startswith(ION_MOBILITY_STUDY_TEXT_CODE) for item in written["warnings"]))
            self.assertEqual(unit_id, written["analysis_unit_id"], "the unit keeps its id")

    def test_mtbks220_is_handed_off_mixed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, unit_id = self.ingest(temporary, mtbks220_view(), "MTBKS220")
            handoff = msdial_catalog_reanalysis_handoff(unit_id, database=database)

            self.assertEqual("Mixed", handoff["technical_settings"]["ion_mobility"])
            self.assertEqual("mixed", handoff["ion_mobility_evidence"]["state"])
            self.assertEqual({"bruker_baf": 2, "bruker_tdf": 3}, handoff["ion_mobility_evidence"]["container_formats"])
            self.assertFalse(
                any(reason.startswith("ion_mobility") for reason in handoff["blocking_reasons"]),
                "Interactive excludes the TDF part; the Catalog does not block the unit",
            )


if __name__ == "__main__":
    unittest.main()

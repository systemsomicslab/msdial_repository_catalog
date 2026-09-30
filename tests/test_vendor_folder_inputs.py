"""One analysis input per vendor container.

A Waters .raw and an Agilent or Bruker .d are folders, and MetaboBank lists every file inside them.
MTBKS217 positive names twelve folders in its SDRF (`raw/190827_025pp.raw/`) and lists 477 files
beneath them. The Catalog made each of those files an analysis input and a sample of its own, with
no attributes: 477 samples for twelve injections, the twelve real rows and their Factor Values
dropped, and every _FUNC*.DAT marked for conversion because ".dat" alone is unreadable. On
2026-09-30 the user decided that a folder is one data file. These tests hold the Catalog to that:
one folder, one analysis input, one sample row, with the files inside kept for download as members.

The fixtures are synthetic and shaped on the units named in each test; nothing here reads a
repository or the catalog database.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path

from msdial_repository_catalog.class_proposal import (
    ANALYSIS_INPUT_MODEL,
    VENDOR_FOLDER_MEMBER_ROLE,
    _container_kind,
    analysis_inputs,
    analysis_samples,
    archived_container_of,
    container_format,
    container_of,
    field_based_proposal,
    normalize_analysis_unit,
    normalize_file_roles,
    validate_class_proposal,
)
from msdial_repository_catalog.class_selection import automatic_class_proposal
from msdial_repository_catalog.mcp_server import (
    msdial_catalog_get_analysis_unit,
    msdial_catalog_reanalysis_handoff,
    msdial_catalog_search,
)
from msdial_repository_catalog.models import ClassAssignment, ClassProposal
from msdial_repository_catalog.normalize import project_to_study
from msdial_repository_catalog.storage import Catalog


# The worked example: MTBKS217 positive (5b635e6fc36ea3042e2e), its twelve SDRF rows and the
# number of files MetaboBank lists inside each folder.
MTBKS217_POSITIVE = (
    ("standard sample", "190827_025pp", 30, "standard"),
    ("Blank_sample_1", "190827_026pp", 39, "blank"),
    ("CSRSPlant_Arabi01", "190827_027pp", 42, "Arabidopsis thaliana"),
    ("CSRSPlant_Arabi02", "190827_028pp", 42, "Arabidopsis thaliana"),
    ("CSRSPlant_Arabi03", "190827_029pp", 42, "Arabidopsis thaliana"),
    ("CSRSPlant_Ine01", "190827_030pp", 42, "Oryza sativa"),
    ("CSRSPlant_Ine02", "190827_031pp", 42, "Oryza sativa"),
    ("CSRSPlant_Ine03", "190827_032pp", 42, "Oryza sativa"),
    ("CSRSPlant_Nasu01", "190827_033pp", 42, "Solanum melongena"),
    ("CSRSPlant_Nasu02", "190827_034pp", 42, "Solanum melongena"),
    ("CSRSPlant_Nasu03", "190827_035pp", 42, "Solanum melongena"),
    ("Blank_sample_2", "z_011pp", 30, "blank"),
)


def waters_members(folder: str, count: int) -> list[str]:
    """The files a Waters .raw folder holds, in the proportions MTBKS217 lists them.

    Thirteen chromatogram traces, four information files, a header, and three files per function:
    a 30-file folder has four functions, a 39-file one seven, a 42-file one eight.
    """
    functions = {30: (1, 2, 3, 8), 39: (1, 2, 3, 4, 5, 6, 8), 42: (1, 2, 3, 4, 5, 6, 7, 8)}[count]
    names = [f"_CHRO{index:03d}.DAT" for index in range(1, 14)]
    names += ["_CHROMS.INF", "_FUNCTNS.INF", "_HEADER.TXT", "_INLET.INF", "_extern.inf"]
    for function in functions:
        names += [f"_FUNC{function:03d}.{suffix}" for suffix in ("DAT", "IDX", "STS")]
    return [f"{folder}/{name}" for name in names]


def bruker_members(folder: str, binary: str) -> list[str]:
    """A Bruker .d: its 0-byte marker file named after the folder, a method folder, the data."""
    name = folder.rsplit("/", 1)[-1]
    return [
        f"{folder}/{name}",
        f"{folder}/5512.m/lock.file",
        f"{folder}/5512.m/submethods.xml",
        f"{folder}/SampleInfo.xml",
        f"{folder}/{binary}",
        f"{folder}/{binary}_idx" if binary.endswith(".baf") else f"{folder}/{binary}_bin",
    ]


def file_rows(paths: list[str], size: int = 1000, **extra: object) -> list[dict]:
    return [
        {
            "name": path,
            "size_bytes": size,
            "role": "raw",
            "checksum": f"{index:032x}",
            "url": f"https://example.org/study/{path}",
            **extra,
        }
        for index, path in enumerate(paths)
    ]


def mtbks217_unit() -> dict:
    rows = []
    files = []
    for sample_id, stem, count, organism in MTBKS217_POSITIVE:
        folder = f"raw/{stem}.raw"
        rows.append(
            {
                "sample_id": sample_id,
                "raw_file": folder + "/",
                "values": {
                    "Factor Value[organism]": organism,
                    "Characteristics[organism]": organism,
                    "Parameter Value[Scan polarity]": "Positive",
                },
            }
        )
        files.extend(file_rows(waters_members(folder, count)))
    return {
        "source_subrecord_id": "pos",
        "label": "LC-MS / Reversed phase / Positive / DIA",
        "separation": "LC-MS",
        "chromatography": "Reversed phase",
        "ion_mode": "Positive",
        "acquisition_mode": "DIA",
        "target_omics": "Metabolomics",
        "untargeted": True,
        "sample_metadata": rows,
        "files": files,
    }


def as_view(payload: dict) -> dict:
    """The unit as the adapters store it: sample rows with attributes, files with paths."""
    return {
        "unit_id": "fixture",
        "samples": [
            {"sample_id": row["sample_id"], "raw_file": row["raw_file"], "attributes": dict(row["values"])}
            for row in payload["sample_metadata"]
        ],
        "files": [
            {
                "path": item["name"],
                "role": item["role"],
                "size_bytes": item["size_bytes"],
                "checksum": item.get("checksum", ""),
                "download_url": item["url"],
                "sample_id": item.get("sample_id", ""),
            }
            for item in payload["files"]
        ],
    }


def unit_of(rows: list[tuple[str, str]], paths: list[str]) -> dict:
    return {
        "unit_id": "fixture",
        "samples": [{"sample_id": sample_id, "raw_file": raw_file, "attributes": {}} for sample_id, raw_file in rows],
        "files": [{"path": path, "role": "raw", "size_bytes": 10, "sample_id": ""} for path in paths],
    }


def codes(view: dict, blocking: bool | None = None) -> list[str]:
    return [
        item["code"]
        for item in view["analysis_input_issues"]
        if blocking is None or item["blocking"] is blocking
    ]


class ContainerNameTests(unittest.TestCase):
    def test_the_outermost_vendor_segment_is_the_container(self) -> None:
        self.assertEqual("raw/190827_025pp.raw", container_of("raw/190827_025pp.raw/_FUNC001.DAT"))
        self.assertEqual("raw/x.d", container_of("raw/x.d/x.d"), "the Bruker marker file")
        self.assertEqual("raw/x.d", container_of("raw/x.d/5512.m/lock.file"))
        self.assertEqual("raw/x.d", container_of("raw/x.d/inner.d/AcqData/MSScan.bin"), "outermost wins")
        self.assertEqual(
            "raw/rawdata/580_001_001.raw", container_of("raw/rawdata/580_001_001.raw/_FUNC001.DAT")
        )
        self.assertEqual("raw/x.raw", container_of("raw\\x.raw\\_FUNC001.DAT"), "Windows separators")

    def test_a_file_is_not_its_own_container(self) -> None:
        self.assertEqual("", container_of("raw/sample.raw"), "a Thermo .raw is a file")
        self.assertEqual("", container_of("raw/sample.mzML"))
        self.assertEqual("", container_of("raw/rawdata/sample.cdf"), "a directory named raw is not a .raw")

    def test_a_packed_container_is_the_container_it_unpacks_to(self) -> None:
        self.assertEqual(
            "FILES/RAW_FILES/P_Lipids_PL079/P_Lipids_PL079_B1_001_PQC1.d",
            archived_container_of("FILES/RAW_FILES/P_Lipids_PL079/P_Lipids_PL079_B1_001_PQC1.d.zip"),
        )
        for name, inner in (
            ("x.raw.zip", "x.raw"), ("x.d.rar", "x.d"), ("x.raw.rar", "x.raw"), ("x.d.7z", "x.d"),
            ("x.d.tar.gz", "x.d"), ("x.d.tgz", "x.d"), ("x.wiff.zip", "x.wiff"),
            ("x.mzML.gz", "x.mzML"), ("x.cdf.zip", "x.cdf"), ("x.raw.gz", "x.raw"),
        ):
            self.assertEqual(inner, archived_container_of(name), name)
        for name in ("ST000001_Rawdata.zip", "x.tar.gz", "x.xlsx.zip", "x.17.7z", "x.mzML", ".d.zip"):
            self.assertEqual("", archived_container_of(name), name)

    def test_a_packed_container_is_ranked_as_its_inner_kind(self) -> None:
        self.assertEqual("vendor", _container_kind("x.raw.zip"))
        self.assertEqual("vendor", _container_kind("x.d.rar"))
        self.assertEqual("converted", _container_kind("x.mzML.gz"))
        self.assertEqual("unreadable", _container_kind("x.mzXML.zip"))

    def test_the_format_is_read_from_member_names(self) -> None:
        self.assertEqual("waters_raw", container_format(["_FUNC001.DAT", "_HEADER.TXT"]))
        self.assertEqual("agilent_d", container_format(["AcqData/MSScan.bin", "AcqData/Contents.xml"]))
        self.assertEqual("bruker_tdf", container_format(["x.d", "analysis.tdf", "analysis.tdf_bin"]))
        self.assertEqual("bruker_tsf", container_format(["analysis.tsf"]))
        self.assertEqual("bruker_baf", container_format(["analysis.baf", "5512.m/lock.file"]))
        self.assertEqual("", container_format(["acqu", "fid"]), "a name that says nothing")
        self.assertEqual("", container_format(["sub/_FUNC001.DAT"]), "only the folder's own files count")


class Mtbks217Tests(unittest.TestCase):
    """THE WORKED EXAMPLE: twelve Waters folders, 477 files, twelve samples."""

    def setUp(self) -> None:
        self.view = normalize_analysis_unit(as_view(mtbks217_unit()))

    def test_twelve_inputs_twelve_samples(self) -> None:
        inputs = self.view["analysis_inputs"]

        self.assertEqual(12, len(inputs))
        self.assertEqual(12, self.view["analysis_file_count"])
        self.assertEqual(12, self.view["sample_count"])
        self.assertEqual({"vendor_folder"}, {item["kind"] for item in inputs})
        self.assertEqual({"waters_raw"}, {item["format"] for item in inputs})
        self.assertEqual(477, sum(item["member_count"] for item in inputs))
        self.assertEqual(
            [f"raw/{stem}.raw" for _, stem, _, _ in sorted(MTBKS217_POSITIVE, key=lambda row: row[1])],
            [item["path"] for item in inputs],
        )
        self.assertEqual([], codes(self.view))

    def test_the_real_rows_survive_with_their_factor_values(self) -> None:
        """The twelve SDRF rows, not 477 attribute-less rows named after member files."""
        samples = {item["sample_id"]: item for item in self.view["samples"]}

        self.assertEqual({row[0] for row in MTBKS217_POSITIVE}, set(samples))
        self.assertEqual("Oryza sativa", samples["CSRSPlant_Ine02"]["attributes"]["Factor Value[organism]"])
        # The folder, with no trailing slash: raw/x.raw/ has the basename "", which is what made
        # every real row unmatchable and what the gate's CLS-2 reads.
        self.assertEqual("raw/190827_031pp.raw", samples["CSRSPlant_Ine02"]["raw_file"])

    def test_the_members_stay_listed_for_download(self) -> None:
        files = self.view["files"]
        by_container: dict[str, list[dict]] = {}
        for item in files:
            by_container.setdefault(item["container"], []).append(item)
        sample_of = {f"raw/{stem}.raw": sample_id for sample_id, stem, _, _ in MTBKS217_POSITIVE}

        self.assertEqual(477, len(files))
        self.assertEqual({VENDOR_FOLDER_MEMBER_ROLE}, {item["role"] for item in files})
        self.assertEqual(
            {f"raw/{stem}.raw": count for _, stem, count, _ in MTBKS217_POSITIVE},
            {key: len(value) for key, value in by_container.items()},
        )
        for item in files:
            self.assertEqual(sample_of[item["container"]], item["sample_id"], item["path"])
            self.assertTrue(item["sample_id_resolved"])
            self.assertNotIn("requires_conversion", item, "a _FUNC*.DAT is a member, not an input")
            self.assertNotIn("demoted_because", item)

    def test_a_class_proposal_is_one_assignment_per_folder(self) -> None:
        unit = {**as_view(mtbks217_unit()), "unit_id": "5b635e6fc36ea3042e2e"}

        proposal = field_based_proposal(unit, "Compare plant species.", ["Factor Value[organism]"])
        self.assertEqual(12, len(proposal.assignments))

        per_member = ClassProposal(
            proposal_id="per-member",
            unit_id=unit["unit_id"],
            purpose="Compare plant species.",
            selected_fields=["Factor Value[organism]"],
            assignments=[
                ClassAssignment(sample_id=item["path"].casefold(), class_label="x", values={})
                for item in unit["files"]
            ],
            rationale="The old projection's 477 samples.",
        )
        with self.assertRaisesRegex(ValueError, "unknown samples"):
            validate_class_proposal(unit, per_member)

    def test_projecting_the_view_again_changes_nothing(self) -> None:
        again = normalize_analysis_unit(self.view)

        self.assertEqual(self.view["samples"], again["samples"])
        self.assertEqual(self.view["analysis_inputs"], again["analysis_inputs"])
        self.assertEqual(self.view["files"], again["files"])
        self.assertEqual(self.view["analysis_input_issues"], again["analysis_input_issues"])
        self.assertEqual(analysis_samples(self.view), self.view["samples"])

    def test_without_a_sample_table_a_member_keeps_its_sample(self) -> None:
        files = normalize_file_roles(self.view["files"])

        self.assertEqual(
            [item["sample_id"] for item in self.view["files"]], [item["sample_id"] for item in files]
        )


class BrukerTests(unittest.TestCase):
    """MTBKS219: Bruker .d folders, each with a marker file and a method folder, BAF and TDF mixed."""

    def unit(self, baf: int, tdf: int) -> dict:
        rows = []
        paths: list[str] = []
        for index in range(baf + tdf):
            mode, binary = ("timsOFF", "analysis.baf") if index < baf else ("timsON", "analysis.tdf")
            folder = f"raw/AG_{index}_{mode}_pos_1-29_1_{5000 + index}.d"
            rows.append((f"Brukermouse_AG_{index // 3}", folder + "/"))
            paths += bruker_members(folder, binary)
        return unit_of(rows, paths)

    def test_the_marker_file_is_a_member_not_a_sample(self) -> None:
        view = normalize_analysis_unit(self.unit(2, 0))

        self.assertEqual(2, len(view["analysis_inputs"]))
        self.assertEqual(2, view["sample_count"])
        self.assertTrue(all(item["path"].endswith(".d") and item["path"].count("/") == 1 for item in view["analysis_inputs"]))
        self.assertEqual({"bruker_baf"}, {item["format"] for item in view["analysis_inputs"]})
        markers = [
            item for item in view["files"]
            if item["path"].rsplit("/", 1)[-1] == item["container"].rsplit("/", 1)[-1]
        ]
        self.assertEqual(2, len(markers))
        self.assertEqual({VENDOR_FOLDER_MEMBER_ROLE}, {item["role"] for item in markers})

    def test_mixed_formats_warn_and_hint_a_split_but_do_not_block(self) -> None:
        """MTBKS219 positive: 50 BAF and 102 TDF. A blocker would take it out of the campaign."""
        view = normalize_analysis_unit(self.unit(50, 102))

        self.assertEqual(152, len(view["analysis_inputs"]))
        self.assertEqual([], codes(view, blocking=True))
        self.assertEqual(["mixed_container_formats"], codes(view, blocking=False))
        warning = view["analysis_input_issues"][0]
        self.assertEqual({"bruker_baf": 50, "bruker_tdf": 102}, warning["formats"])
        self.assertEqual({"bruker_baf": 50, "bruker_tdf": 102}, view["split_hint"]["groups"])
        self.assertEqual("format", view["split_hint"]["key"])

    def test_a_folder_whose_names_say_no_format_is_unknown_in_both_places(self) -> None:
        """The split hint is keyed on each input's format, so the two must spell unknown alike."""
        unit = self.unit(2, 2)
        unit["files"] += [
            {"path": path, "role": "raw", "size_bytes": 10, "sample_id": ""}
            for path in ("raw/odd.d/odd.d", "raw/odd.d/SampleInfo.xml")
        ]
        unit["samples"].append({"sample_id": "odd", "raw_file": "raw/odd.d/", "attributes": {}})
        view = normalize_analysis_unit(unit)

        self.assertEqual({"bruker_baf": 2, "bruker_tdf": 2, "unknown": 1}, view["split_hint"]["groups"])
        self.assertEqual(
            view["split_hint"]["groups"], dict(Counter(item["format"] for item in view["analysis_inputs"]))
        )

    def test_one_format_gives_no_hint(self) -> None:
        view = normalize_analysis_unit(self.unit(0, 4))

        self.assertIsNone(view["split_hint"])
        self.assertEqual([], codes(view))

    def test_a_sample_with_three_folders_takes_one_class(self) -> None:
        """MTBKS219 names each tissue three times, once per folder; Interactive refuses a double."""
        unit = self.unit(3, 3)
        for row in unit["samples"]:
            row["attributes"] = {"Factor Value[tissue]": "brain" if row["sample_id"].endswith("0") else "liver"}

        proposal = field_based_proposal(unit, "Compare tissues.", ["Factor Value[tissue]"])
        selected, decision = automatic_class_proposal(unit, "Compare tissues.")

        self.assertEqual(6, len(analysis_samples(unit)))
        self.assertEqual(["Brukermouse_AG_0", "Brukermouse_AG_1"], [item.sample_id for item in proposal.assignments])
        self.assertEqual("declared", decision["decision"])
        self.assertEqual(2, len(selected.assignments))

    def test_rows_of_one_sample_that_disagree_are_refused(self) -> None:
        unit = self.unit(1, 1)
        unit["samples"] = [{**row, "sample_id": "same"} for row in unit["samples"]]
        unit["samples"][0]["attributes"] = {"Factor Value[tissue]": "brain"}
        unit["samples"][1]["attributes"] = {"Factor Value[tissue]": "liver"}

        with self.assertRaisesRegex(ValueError, "disagree"):
            field_based_proposal(unit, "Compare tissues.", ["Factor Value[tissue]"])


class OtherShapeTests(unittest.TestCase):
    def test_agilent_acqdata(self) -> None:
        paths = [f"raw/s{index}.d/AcqData/{name}" for index in (1, 2) for name in ("MSScan.bin", "Contents.xml")]
        view = normalize_analysis_unit(unit_of([("s1", "raw/s1.d/"), ("s2", "raw/s2.d/")], paths))

        self.assertEqual(["agilent_d", "agilent_d"], [item["format"] for item in view["analysis_inputs"]])
        self.assertEqual(["s1", "s2"], [item["sample_id"] for item in view["analysis_inputs"]])

    def test_a_folder_nested_in_a_directory(self) -> None:
        """MTBKS47: raw/rawdata/580_001_001.raw/..."""
        rows = [(f"G01_{index}", f"raw/rawdata/580_001_00{index}.raw/") for index in (1, 2, 3)]
        paths = [
            f"raw/rawdata/580_001_00{index}.raw/{name}"
            for index in (1, 2, 3)
            for name in ("_FUNC001.DAT", "_FUNC001.IDX", "_HEADER.TXT")
        ]
        view = normalize_analysis_unit(unit_of(rows, paths))

        self.assertEqual(3, len(view["analysis_inputs"]))
        self.assertEqual(3, view["sample_count"])
        self.assertEqual([], codes(view))

    def test_folders_beside_wiff_files(self) -> None:
        """MTBKS222: nine Waters folders and twelve .wiff files with their .wiff.scan sidecars."""
        rows = [(f"folder_{index}", f"raw/w{index}.raw/") for index in range(9)]
        rows += [(f"wiff_{index}", f"raw/s{index}.wiff") for index in range(12)]
        paths = [f"raw/w{index}.raw/{name}" for index in range(9) for name in ("_FUNC001.DAT", "_HEADER.TXT")]
        paths += [f"raw/s{index}{suffix}" for index in range(12) for suffix in (".wiff", ".wiff.scan")]
        view = normalize_analysis_unit(unit_of(rows, paths))

        self.assertEqual(21, len(view["analysis_inputs"]))
        self.assertEqual(21, view["sample_count"])
        self.assertEqual(9, sum(item["kind"] == "vendor_folder" for item in view["analysis_inputs"]))
        self.assertEqual(12, sum(item["kind"] == "file" for item in view["analysis_inputs"]))
        sidecars = [item for item in view["files"] if item["role"] == "sidecar"]
        self.assertEqual(12, len(sidecars))

    def test_a_vendor_folder_beats_an_mzml_of_the_same_sample(self) -> None:
        """The folder competes as the vendor container it is, as a listed x.raw file would."""
        view = normalize_analysis_unit(
            unit_of([("x", "raw/x.raw/")], ["raw/x.raw/_FUNC001.DAT", "raw/x.raw/_HEADER.TXT", "raw/x.mzML"])
        )
        roles = {item["path"]: item["role"] for item in view["files"]}

        self.assertEqual("raw_alternate", roles["raw/x.mzML"])
        self.assertEqual(["raw/x.raw"], [item["path"] for item in view["analysis_inputs"]])

    def test_a_per_sample_archive(self) -> None:
        """MetaboLights MTBLS7260: FILES/RAW_FILES/.../x.d.zip, one per sample."""
        base = "FILES/RAW_FILES/P_LIPIDS/P_Lipids_PL079"
        names = ["P_Lipids_PL079_B1_001_PQC1", "P_Lipids_PL079_B1_002_HQC"]
        view = normalize_analysis_unit(
            unit_of([(name, f"{base}/{name}.d.zip") for name in names], [f"{base}/{name}.d.zip" for name in names])
        )
        inputs = view["analysis_inputs"]

        self.assertEqual(["archived_container"] * 2, [item["kind"] for item in inputs])
        self.assertEqual([f"{base}/{name}.d" for name in names], [item["path"] for item in inputs])
        self.assertEqual([f"{base}/{name}.d.zip" for name in names], [item["archive"] for item in inputs])
        self.assertEqual(names, [item["sample_id"] for item in inputs])
        self.assertEqual([f"{base}/{name}.d" for name in names], [item["container"] for item in view["files"]])
        # The sample keeps the published file's name; the archive is what is downloaded.
        self.assertEqual([f"{base}/{name}.d.zip" for name in names], [item["raw_file"] for item in view["samples"]])

    def test_a_packed_vendor_container_beats_a_packed_mzml(self) -> None:
        """MTBLS243 publishes each sample as x.d.zip and again as x.mzML.zip: 44 inputs for 22."""
        view = normalize_analysis_unit(
            unit_of([("a", "FILES/a.d.zip")], ["FILES/a.d.zip", "FILES/a.mzML.zip"])
        )
        roles = {item["path"]: item["role"] for item in view["files"]}

        self.assertEqual("raw", roles["FILES/a.d.zip"])
        self.assertEqual("raw_alternate", roles["FILES/a.mzML.zip"])
        self.assertEqual(1, view["analysis_file_count"])

    def test_a_folder_listed_beside_its_own_archive(self) -> None:
        """raw/x.raw/ member by member and raw/x.raw.zip: one container, so one input."""
        view = normalize_analysis_unit(
            unit_of([("x", "raw/x.raw/")], ["raw/x.raw/_FUNC001.DAT", "raw/x.raw/_HEADER.TXT", "raw/x.raw.zip"])
        )
        archive = next(item for item in view["files"] if item["path"] == "raw/x.raw.zip")

        self.assertEqual(
            [("raw/x.raw", "vendor_folder", "x")],
            [(item["path"], item["kind"], item["sample_id"]) for item in view["analysis_inputs"]],
        )
        self.assertEqual("raw_alternate", archive["role"])
        self.assertIn("listed unpacked beside it", archive["demoted_because"])
        self.assertNotIn("container", archive, "an alternate is not a member of the folder")
        self.assertEqual([], codes(view))

    def test_a_file_listed_beside_its_own_archive(self) -> None:
        view = normalize_analysis_unit(unit_of([("x", "raw/x.mzML")], ["raw/x.mzML", "raw/x.mzML.gz"]))
        roles = {item["path"]: item["role"] for item in view["files"]}

        self.assertEqual({"raw/x.mzML": "raw", "raw/x.mzML.gz": "raw_alternate"}, roles)
        self.assertEqual(["raw/x.mzML"], [item["path"] for item in view["analysis_inputs"]])

    def test_an_input_carries_its_conversion_target(self) -> None:
        """MTBLS688 publishes only x.mzXML.lzma: packed mzXML, to be converted to mzML and run."""
        view = normalize_analysis_unit(
            unit_of([], ["FILES/a.mzXML", "FILES/b.mzXML.gz", "FILES/c.mzXML.lzma", "FILES/d.mzData"])
        )
        inputs = {item["path"]: item for item in view["analysis_inputs"]}

        self.assertEqual(["FILES/a.mzXML", "FILES/b.mzXML", "FILES/c.mzXML", "FILES/d.mzData"], list(inputs))
        self.assertEqual("archived_container", inputs["FILES/c.mzXML"]["kind"])
        self.assertEqual(".mzxml", inputs["FILES/c.mzXML"]["suffix"])
        for path in ("FILES/a.mzXML", "FILES/b.mzXML", "FILES/c.mzXML"):
            self.assertTrue(inputs[path]["requires_conversion"], path)
            self.assertEqual("mzML", inputs[path]["conversion_target"], path)
        self.assertTrue(inputs["FILES/d.mzData"]["requires_conversion"])
        self.assertEqual("", inputs["FILES/d.mzData"]["conversion_target"], "no conversion is planned")

    def test_a_packed_sidecar_is_a_sidecar(self) -> None:
        """s.wiff.scan.zip was an input of its own, flagged for conversion: Interactive excluded the unit."""
        unit = unit_of([("s", "raw/s.wiff")], ["raw/s.wiff", "raw/s.wiff.scan.zip"])
        unit["files"][0]["sample_id"] = "s"
        view = normalize_analysis_unit(unit)
        sidecar = next(item for item in view["files"] if item["path"] == "raw/s.wiff.scan.zip")

        self.assertEqual(["raw/s.wiff"], [item["path"] for item in view["analysis_inputs"]])
        self.assertEqual(1, view["sample_count"])
        self.assertEqual("sidecar", sidecar["role"])
        self.assertEqual("raw/s.wiff.scan", sidecar["unpacks_to"])
        self.assertNotIn("requires_conversion", sidecar)
        self.assertEqual("s", sidecar["sample_id"])
        self.assertEqual("raw/s.wiff", sidecar["parent_file"])
        self.assertEqual(["raw/s.wiff.scan.zip"], view["samples"][0]["related_files"])

    def test_folders_beside_vendor_files_of_another_suffix_warn(self) -> None:
        """MTBKS222: nine Waters folders and twelve .wiff files in one unit, two vendors' readers."""
        rows = [(f"folder_{index}", f"raw/w{index}.raw/") for index in range(3)]
        rows += [(f"wiff_{index}", f"raw/s{index}.wiff") for index in range(4)]
        paths = [f"raw/w{index}.raw/{name}" for index in range(3) for name in ("_FUNC001.DAT", "_HEADER.TXT")]
        paths += [f"raw/s{index}{suffix}" for index in range(4) for suffix in (".wiff", ".wiff.scan")]
        view = normalize_analysis_unit(unit_of(rows, paths))

        self.assertEqual([], codes(view, blocking=True))
        self.assertEqual(["mixed_container_suffixes"], codes(view, blocking=False))
        self.assertEqual({".raw": 3, ".wiff": 4}, view["analysis_input_issues"][0]["suffixes"])
        self.assertEqual("suffix", view["split_hint"]["key"])
        self.assertEqual({".raw": 3, ".wiff": 4}, view["split_hint"]["groups"])
        self.assertEqual(
            view["split_hint"]["groups"],
            dict(Counter(item[view["split_hint"]["key"]] for item in view["analysis_inputs"])),
        )

    def test_folders_beside_files_of_their_own_suffix_or_converted_files_do_not_warn(self) -> None:
        view = normalize_analysis_unit(
            unit_of([("a", "raw/a.d/"), ("b", "raw/b.mzML")], ["raw/a.d/AcqData/MSScan.bin", "raw/b.mzML"])
        )

        self.assertEqual([], codes(view))
        self.assertIsNone(view["split_hint"])

    def test_an_archive_two_rows_name_is_read_as_a_file_is(self) -> None:
        """A published archive is one file, and the Catalog has always given a file to its first row."""
        view = normalize_analysis_unit(
            unit_of([("a", "FILES/a.raw.zip"), ("b", "FILES/a.raw.zip")], ["FILES/a.raw.zip"])
        )

        self.assertEqual(["a"], [item["sample_id"] for item in view["samples"]])
        self.assertEqual([], codes(view))


class BlockingTests(unittest.TestCase):
    def test_a_folder_two_rows_name_blocks_without_a_merge(self) -> None:
        """MTBKS212: eight sample rows, seven folders."""
        rows = [("SQ_1", "raw/fig_2.raw/"), ("SQ_2", "raw/fig_2.raw/"), ("SQ_3", "raw/fig_3.raw/")]
        paths = [f"raw/{name}.raw/_FUNC001.DAT" for name in ("fig_2", "fig_3")]
        view = normalize_analysis_unit(unit_of(rows, paths))

        self.assertEqual(["container_shared_by_samples"], codes(view, blocking=True))
        self.assertEqual(2, len(view["analysis_inputs"]), "one input per folder, never duplicated")
        shared = view["analysis_inputs"][0]
        self.assertEqual("", shared["sample_id"])
        self.assertEqual(["SQ_1", "SQ_2"], shared["sample_ids"])
        self.assertEqual(["SQ_1", "SQ_2", "SQ_3"], [item["sample_id"] for item in view["samples"]], "no row merged")
        member = next(item for item in view["files"] if item["container"] == "raw/fig_2.raw")
        self.assertFalse(member["sample_id_resolved"])
        self.assertEqual(codes(view), codes(normalize_analysis_unit(view)))

    def test_a_row_naming_a_folder_the_listing_lacks(self) -> None:
        rows = [("a", "raw/a.raw/"), ("b", "raw/b.raw/")]
        view = normalize_analysis_unit(unit_of(rows, ["raw/a.raw/_FUNC001.DAT"]))

        self.assertEqual(["sample_without_container"], codes(view, blocking=True))
        self.assertEqual(["a", "b"], [item["sample_id"] for item in view["samples"]], "kept, so it is seen")
        self.assertEqual(1, len(view["analysis_inputs"]))
        self.assertEqual(codes(view), codes(normalize_analysis_unit(view)))

    def test_a_listed_folder_no_row_names(self) -> None:
        view = normalize_analysis_unit(
            unit_of([("a", "raw/a.raw/")], ["raw/a.raw/_FUNC001.DAT", "raw/b.raw/_FUNC001.DAT"])
        )

        self.assertEqual(["container_without_sample"], codes(view, blocking=True))
        self.assertEqual(["a"], [item["sample_id"] for item in view["samples"]], "no row is invented")
        self.assertEqual(codes(view), codes(normalize_analysis_unit(view)))

    def test_a_declared_directory_is_one_sample_and_blocks(self) -> None:
        """MTBKS225: Bruker NMR experiment folders such as raw/0h_rep1/."""
        rows = [(f"{hour}h_rep{rep}", f"raw/{hour}h_rep{rep}/") for hour in (0, 24, 48) for rep in (1, 2, 3)]
        paths = [
            f"{raw_file}{name}"
            for _, raw_file in rows
            for name in ("acqu", "acqus", "fid", "pdata/20/1r", "pdata/20/1i")
        ]
        view = normalize_analysis_unit(unit_of(rows, paths))

        self.assertEqual(9, view["sample_count"])
        self.assertEqual({"declared_directory"}, {item["kind"] for item in view["analysis_inputs"]})
        self.assertEqual(["declared_directory_not_msdial_input"], codes(view, blocking=True))
        self.assertEqual({"directory_member"}, {item["role"] for item in view["files"]})
        self.assertEqual(codes(view), codes(normalize_analysis_unit(view)))


class AttributionTests(unittest.TestCase):
    def test_an_exact_path_beats_an_earlier_row_of_the_same_name(self) -> None:
        """MTBLS7429: plate1/S_29_01.raw was given to a plate-2 row that came first in the table."""
        rows = [("P2_S", "FILES/plate2/S_29_01.raw"), ("S_29_01", "FILES/plate1/S_29_01.raw")]
        view = normalize_analysis_unit(unit_of(rows, ["FILES/plate1/S_29_01.raw", "FILES/plate2/S_29_01.raw"]))

        self.assertEqual(
            {"FILES/plate1/S_29_01.raw": "S_29_01", "FILES/plate2/S_29_01.raw": "P2_S"},
            {item["raw_file"]: item["sample_id"] for item in view["samples"]},
        )

    def test_a_basename_names_a_file_only_when_one_row_has_it(self) -> None:
        unique = normalize_analysis_unit(unit_of([("s", "x.mzML")], ["FILES/sub/x.mzML"]))
        self.assertEqual(["s"], [item["sample_id"] for item in unique["samples"]])

        nested = normalize_analysis_unit(unit_of([("s", "sub/x.mzML")], ["FILES/sub/x.mzML"]))
        self.assertEqual(["s"], [item["sample_id"] for item in nested["samples"]])

        ambiguous = normalize_analysis_unit(
            unit_of([("a", "one/x.mzML"), ("b", "two/x.mzML")], ["FILES/three/x.mzML"])
        )
        self.assertNotIn(ambiguous["samples"][0]["sample_id"], {"a", "b"})

    def test_every_exact_path_is_claimed_before_any_fallback(self) -> None:
        """The input whose path nests the row's sorts first, and used to take the row by name."""
        view = normalize_analysis_unit(unit_of([("exact", "raw/x.mzML")], ["FILES/raw/x.mzML", "raw/x.mzML"]))
        by_path = {item["path"]: item["sample_id"] for item in view["analysis_inputs"]}

        self.assertEqual("exact", by_path["raw/x.mzML"])
        self.assertNotEqual("exact", by_path["FILES/raw/x.mzML"])
        self.assertEqual(1, [item["sample_id"] for item in view["samples"]].count("exact"))
        self.assertEqual(
            ["FILES/raw/x.mzML", "raw/x.mzML"], [item["raw_file"] for item in view["samples"]], "input order"
        )

    def test_a_unique_basename_does_not_take_a_row_another_input_names_exactly(self) -> None:
        """Plate 1 sorts first; the one row names the plate-2 file, and its basename is unique."""
        rows = [("P2_S", "FILES/plate2/S_29_01.raw")]
        view = normalize_analysis_unit(unit_of(rows, ["FILES/plate1/S_29_01.raw", "FILES/plate2/S_29_01.raw"]))
        by_path = {item["path"]: item["sample_id"] for item in view["analysis_inputs"]}

        self.assertEqual("P2_S", by_path["FILES/plate2/S_29_01.raw"])
        self.assertNotEqual("P2_S", by_path["FILES/plate1/S_29_01.raw"])

    def test_a_declared_sample_id_does_not_take_a_row_another_input_names_exactly(self) -> None:
        """a.mzML carries b's sample id; the row, and its attributes, stay with the file it names."""
        unit = unit_of([("b", "FILES/b.mzML")], ["FILES/a.mzML", "FILES/b.mzML"])
        unit["samples"][0]["attributes"] = {"Factor Value[genotype]": "wild type"}
        unit["files"][0]["sample_id"] = "b"
        samples = {item["raw_file"]: item for item in analysis_samples(unit)}

        self.assertEqual({"Factor Value[genotype]": "wild type"}, samples["FILES/b.mzML"]["attributes"])
        self.assertEqual({}, samples["FILES/a.mzML"]["attributes"])


class ParentDirectoryTests(unittest.TestCase):
    """A row naming the directory the other rows' inputs sit in (raw/) names no sample of its own.

    It used to become a declared directory that absorbed every file below it, the other rows' own
    files included, and blocked the unit as a directory MS-DIAL cannot open. On main it was dropped.
    """

    def test_a_parent_directory_row_is_dropped_with_a_warning(self) -> None:
        rows = [("study", "raw/"), ("a", "raw/a.mzML"), ("b", "raw/b.mzML")]
        view = normalize_analysis_unit(unit_of(rows, ["raw/a.mzML", "raw/b.mzML"]))

        self.assertEqual(
            [("raw/a.mzML", "file", "a"), ("raw/b.mzML", "file", "b")],
            [(item["path"], item["kind"], item["sample_id"]) for item in view["analysis_inputs"]],
        )
        self.assertEqual(["a", "b"], [item["sample_id"] for item in view["samples"]])
        self.assertEqual({"raw"}, {item["role"] for item in view["files"]})
        self.assertEqual([], codes(view, blocking=True))
        self.assertEqual(["parent_directory_row"], codes(view, blocking=False))
        self.assertEqual(["raw/"], view["analysis_input_issues"][0]["examples"])
        again = normalize_analysis_unit(view)
        self.assertEqual(view["samples"], again["samples"])
        self.assertEqual(view["analysis_input_issues"], again["analysis_input_issues"])
        self.assertEqual(view["samples"], analysis_samples(view))

    def test_files_other_rows_name_are_never_absorbed(self) -> None:
        """mzXML is no container MS-DIAL opens, and was absorbed as a member of the directory."""
        rows = [("run", "raw/run/"), ("s1", "raw/run/s1.mzXML"), ("s2", "raw/run/s2.mzXML")]
        view = normalize_analysis_unit(unit_of(rows, ["raw/run/notes.txt", "raw/run/s1.mzXML", "raw/run/s2.mzXML"]))
        roles = {item["path"]: item["role"] for item in view["files"]}
        inputs = {item["path"]: item for item in view["analysis_inputs"]}

        self.assertEqual({"raw"}, set(roles.values()), "nothing is a member of the parent directory")
        self.assertEqual("s1", inputs["raw/run/s1.mzXML"]["sample_id"])
        self.assertEqual("s2", inputs["raw/run/s2.mzXML"]["sample_id"])
        self.assertEqual("mzML", inputs["raw/run/s1.mzXML"]["conversion_target"])
        self.assertNotIn("run", [item["sample_id"] for item in view["samples"]])
        self.assertEqual([], codes(view, blocking=True))

    def test_a_parent_directory_of_vendor_folders(self) -> None:
        """The row had no member of its own, and blocked as a sample whose folder is not listed."""
        rows = [("study", "raw/"), ("a", "raw/a.raw/"), ("b", "raw/b.raw/")]
        paths = [f"raw/{name}.raw/{member}" for name in ("a", "b") for member in ("_FUNC001.DAT", "_HEADER.TXT")]
        view = normalize_analysis_unit(unit_of(rows, paths))

        self.assertEqual(["raw/a.raw", "raw/b.raw"], [item["path"] for item in view["analysis_inputs"]])
        self.assertEqual(["a", "b"], [item["sample_id"] for item in view["samples"]])
        self.assertEqual([], codes(view, blocking=True))
        self.assertEqual(["parent_directory_row"], codes(view, blocking=False))

    def test_a_parent_directory_of_sample_directories(self) -> None:
        """The innermost directory a row names is the sample; the one above it is a parent."""
        rows = [("study", "raw/"), ("0h", "raw/0h/"), ("24h", "raw/24h/")]
        paths = [f"raw/{hour}/{name}" for hour in ("0h", "24h") for name in ("acqu", "fid")]
        view = normalize_analysis_unit(unit_of(rows, paths))

        self.assertEqual(["raw/0h", "raw/24h"], [item["path"] for item in view["analysis_inputs"]])
        self.assertEqual(["0h", "24h"], [item["sample_id"] for item in view["analysis_inputs"]])
        self.assertEqual(["declared_directory_not_msdial_input"], codes(view, blocking=True))
        self.assertEqual(["parent_directory_row"], codes(view, blocking=False))
        self.assertEqual(codes(view), codes(normalize_analysis_unit(view)))

    def test_a_directory_row_holding_only_unnamed_files_still_blocks(self) -> None:
        """No other row names anything below it, so it is not a parent: it names what it holds."""
        view = normalize_analysis_unit(unit_of([("run", "raw/run/")], ["raw/run/acqu", "raw/run/fid"]))

        self.assertEqual(["declared_directory_not_msdial_input"], codes(view, blocking=True))
        self.assertEqual(["run"], [item["sample_id"] for item in view["samples"]])


class LinearTimeTests(unittest.TestCase):
    """The old projection compared every file with every row and every file: MTBKS263 never finished."""

    def test_an_nmr_unit_of_27198_files(self) -> None:
        rows = [(f"s{index}", f"raw/NAGAHAMA_{index // 10}/{index % 10}/") for index in range(1236)]
        paths = [f"{raw_file}pdata/1/f{member}" for _, raw_file in rows for member in range(22)]
        paths += [f"raw/NAGAHAMA_0/0/extra{index}" for index in range(27198 - len(paths))]
        self.assertEqual(27198, len(paths))

        started = time.perf_counter()
        view = normalize_analysis_unit(unit_of(rows, paths))
        elapsed = time.perf_counter() - started

        self.assertEqual(1236, view["sample_count"])
        self.assertLess(elapsed, 5.0)

    def test_a_waters_unit_of_11315_files(self) -> None:
        """MTBKS54: 870 folders."""
        rows = [(f"s{index}", f"raw/rawdata/f{index:04d}.raw/") for index in range(870)]
        paths = [f"raw/rawdata/f{index:04d}.raw/_FUNC{member:03d}.DAT" for index in range(870) for member in range(13)]
        paths += [f"raw/rawdata/f0000.raw/_extra{index}.INF" for index in range(11315 - len(paths))]
        self.assertEqual(11315, len(paths))

        started = time.perf_counter()
        view = normalize_analysis_unit(unit_of(rows, paths))
        elapsed = time.perf_counter() - started

        self.assertEqual(870, view["analysis_file_count"])
        self.assertLess(elapsed, 5.0)

    def test_a_file_unit_of_4776_files(self) -> None:
        """MTBLS124 shape: one file per row. The old projection took sixteen seconds."""
        rows = [(f"s{index}", f"FILES/s{index}.mzML") for index in range(4776)]
        started = time.perf_counter()
        view = normalize_analysis_unit(unit_of(rows, [raw_file for _, raw_file in rows]))

        self.assertEqual(4776, view["sample_count"])
        self.assertLess(time.perf_counter() - started, 5.0)


class HandoffTests(unittest.TestCase):
    def ingest(self, temporary: str, unit: dict, repository: str = "metabobank") -> tuple[str, str]:
        database = str(Path(temporary) / "catalog.sqlite")
        study = project_to_study(
            {"repository": repository, "accession": "MTBKS217", "title": "Plant extracts", "analysis_units": [unit]}
        )
        with Catalog(database) as catalog:
            catalog.ingest_study(study)
        return database, study.analysis_units[0].unit_id

    def test_mtbks217_counts_agree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, unit_id = self.ingest(temporary, mtbks217_unit())
            handoff = msdial_catalog_reanalysis_handoff(unit_id, database=database)
            written = json.loads(Path(handoff["handoff_path"]).read_text(encoding="utf-8"))
            files = json.loads(Path(handoff["file_manifest_path"]).read_text(encoding="utf-8"))
            samples = json.loads(Path(handoff["sample_table_path"]).read_text(encoding="utf-8"))
            inputs = json.loads(Path(handoff["analysis_input_manifest_path"]).read_text(encoding="utf-8"))

            self.assertEqual(12, handoff["sample_count"])
            self.assertEqual(12, handoff["analytical_sample_count"])
            self.assertEqual(12, handoff["download_scope"]["analysis_file_count"])
            self.assertEqual(12, handoff["analysis_input_count"])
            self.assertEqual(477, handoff["download_scope"]["file_count"])
            self.assertEqual(477, len(files))
            self.assertEqual(12, len(samples))
            self.assertEqual(ANALYSIS_INPUT_MODEL, handoff["analysis_input_model"])
            self.assertTrue(handoff["analysis_inputs_declared"])
            self.assertEqual(12, len(written["analysis_inputs"]), "inline in the file Interactive reads")
            self.assertEqual(written["analysis_inputs"], inputs)
            self.assertEqual([], handoff["analysis_inputs"], "not relayed through the model")
            self.assertTrue(handoff["analysis_inputs_omitted"])
            self.assertEqual(["class_proposal:missing"], handoff["blocking_reasons"])
            self.assertIsNone(handoff["split_hint"])
            self.assertEqual([], handoff["warnings"])
            self.assertTrue(all(item["attributes"] for item in samples), "the SDRF rows, not fake ones")
            self.assertEqual(
                {item["path"] for item in written["analysis_inputs"]},
                {item["container"] for item in files},
            )

    def test_a_blocked_unit_says_why(self) -> None:
        unit = mtbks217_unit()
        unit["sample_metadata"].append(
            {"sample_id": "ghost", "raw_file": "raw/190827_099pp.raw/", "values": {"Factor Value[organism]": "none"}}
        )
        with tempfile.TemporaryDirectory() as temporary:
            database, unit_id = self.ingest(temporary, unit)
            handoff = msdial_catalog_reanalysis_handoff(unit_id, database=database)

            self.assertIn("analysis_input:sample_without_container", handoff["blocking_reasons"])
            self.assertFalse(handoff["ready_for_download_planning"])
            self.assertIn("disagree", handoff["next_action"])

    def test_a_mixed_unit_carries_its_hint_as_a_warning(self) -> None:
        rows = []
        files = []
        for index in range(4):
            binary = "analysis.baf" if index < 2 else "analysis.tdf"
            folder = f"raw/AG_{index}.d"
            rows.append({"sample_id": f"s{index}", "raw_file": folder + "/", "values": {}})
            files.extend(file_rows(bruker_members(folder, binary)))
        unit = {**mtbks217_unit(), "sample_metadata": rows, "files": files}
        with tempfile.TemporaryDirectory() as temporary:
            database, unit_id = self.ingest(temporary, unit)
            handoff = msdial_catalog_reanalysis_handoff(unit_id, database=database)

            self.assertEqual(["class_proposal:missing"], handoff["blocking_reasons"])
            self.assertEqual({"bruker_baf": 2, "bruker_tdf": 2}, handoff["split_hint"]["groups"])
            self.assertEqual(1, len(handoff["warnings"]))
            self.assertTrue(handoff["warnings"][0].startswith("analysis_input:mixed_container_formats"))

    def test_an_archive_only_unit_declares_no_inputs(self) -> None:
        """A Workbench study's data sit inside its archive: the inputs are found after download."""
        unit = {
            **mtbks217_unit(),
            "sample_metadata": [{"sample_id": "s1", "raw_file": "s1.mzML", "values": {}}],
            "files": [
                {"name": "ST000001_Rawdata.zip", "size_bytes": 10, "role": "raw_archive", "url": "https://example.org/a.zip"}
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            database, unit_id = self.ingest(temporary, unit, repository="metabolomics_workbench")
            handoff = msdial_catalog_reanalysis_handoff(unit_id, database=database)

            self.assertFalse(handoff["analysis_inputs_declared"])
            self.assertEqual(0, handoff["analysis_input_count"])
            self.assertEqual(1, handoff["sample_count"])
            self.assertEqual(0, handoff["analytical_sample_count"])

    def test_the_unit_and_search_views_count_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, unit_id = self.ingest(temporary, mtbks217_unit())
            unit = msdial_catalog_get_analysis_unit(unit_id, database=database)
            full = msdial_catalog_get_analysis_unit(unit_id, database=database, include_inputs=True, input_limit=3)
            search = msdial_catalog_search(accessions=["MTBKS217"], database=database)

            self.assertEqual(12, unit["sample_count"])
            self.assertEqual(477, unit["file_count"])
            self.assertEqual(12, unit["analysis_input_count"])
            self.assertTrue(unit["analysis_inputs_omitted"])
            self.assertEqual(12, len(json.loads(Path(unit["analysis_input_manifest_path"]).read_text(encoding="utf-8"))))
            self.assertEqual(3, len(full["analysis_inputs"]))
            self.assertTrue(full["analysis_inputs_truncated"])
            self.assertEqual(12, search["matches"][0]["sample_count"])
            self.assertEqual(12, search["matches"][0]["analysis_file_count"])
            self.assertLess(len(json.dumps(unit)), 20_000)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

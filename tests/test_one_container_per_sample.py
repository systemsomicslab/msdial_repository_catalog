"""One sample, one analysable container.

MetaboBank MTBKS157 publishes each of its sixteen samples twice, once as .RAW and once as .mzXML,
and both arrived with role "raw" - thirty-two analysis inputs for sixteen samples. MS-DIAL would
have detected every peak twice and aligned each sample against its own second encoding, which looks
like perfect reproducibility and is an artefact of the manifest.

The vendor container wins, on the analyst's instruction of 2026-09-21: its readers are the more
stable of the two in practice. The demoted file keeps role raw_alternate rather than being dropped,
so a run that cannot read the vendor format can still find it.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from msdial_repository_catalog.class_proposal import (
    CONVERTED_SUFFIXES,
    CONVERTIBLE_SUFFIXES,
    UNREADABLE_SUFFIXES,
    VENDOR_RAW_SUFFIXES,
    normalize_analysis_unit,
    normalize_file_roles,
)

VECTORS = Path(__file__).parent / "vectors" / "encoding_preference.v1.json"


def _files(*paths: str) -> list[dict]:
    return normalize_file_roles([{"path": path, "role": "raw"} for path in paths])


def _roles(*paths: str) -> dict[str, str]:
    files = [{"path": path, "role": "raw"} for path in paths]
    return {item["path"]: item["role"] for item in normalize_file_roles(files)}


class VendorWinsTests(unittest.TestCase):
    def test_the_mtbks157_shape(self) -> None:
        """THE REGRESSION, on the unit that exhibits it: sixteen samples, thirty-two inputs."""
        roles = _roles("raw/01026_Bread_nega.RAW", "raw/01026_Bread_nega.mzXML")

        self.assertEqual("raw", roles["raw/01026_Bread_nega.RAW"])
        self.assertEqual("raw_alternate", roles["raw/01026_Bread_nega.mzXML"])

    def test_it_works_for_every_vendor_family_the_campaign_meets(self) -> None:
        for vendor in (".d", ".raw", ".lcd", ".wiff2", ".cdf", ".abf", ".qgd"):
            roles = _roles(f"raw/sample{vendor}", "raw/sample.mzML")
            self.assertEqual("raw", roles[f"raw/sample{vendor}"], vendor)
            self.assertEqual("raw_alternate", roles["raw/sample.mzML"], vendor)

    def test_the_demoted_file_is_kept_and_says_why(self) -> None:
        """Demoted, not dropped: a run that cannot read the vendor format can still find it."""
        files = [
            {"path": "raw/sample.d", "role": "raw"},
            {"path": "raw/sample.mzML", "role": "raw"},
        ]

        result = normalize_file_roles(files)
        demoted = next(item for item in result if item["role"] == "raw_alternate")

        self.assertIn("vendor raw container", demoted["demoted_because"])
        self.assertEqual(2, len(result), "nothing is removed from the manifest")

    def test_a_converted_file_on_its_own_is_still_an_input(self) -> None:
        """Most MetaboLights units publish mzML and nothing else. They must stay analysable."""
        self.assertEqual("raw", _roles("raw/only_open.mzML")["raw/only_open.mzML"])

    def test_a_vendor_file_on_its_own_is_untouched(self) -> None:
        self.assertEqual("raw", _roles("raw/only_vendor.d")["raw/only_vendor.d"])


class NoOpinionTests(unittest.TestCase):
    def test_two_equally_preferred_containers_are_left_alone(self) -> None:
        """Where nobody has stated a preference, this rule does not invent one.

        Two vendor containers of the same sample that are not the .wiff/.wiff2 pair: choosing
        between them would be a preference nobody has expressed, and inventing one is how an
        unevidenced decision enters a pipeline. The .wiff pair is different only because the
        analyst decided it.
        """
        roles = _roles("raw/both_vendor.raw", "raw/both_vendor.d")

        self.assertEqual({"raw"}, set(roles.values()))

    def test_wiff2_wins_over_wiff_always(self) -> None:
        """Decided by the analyst on 2026-09-21, and deliberately not conditional.

        The narrower rule would have been "read the .wiff2 only for SCIEX ZT Scan DIA", which
        needs the acquisition method - something no repository field states and only the .wiff2
        header carries. That would have been a guess wearing the clothes of a decision.
        """
        roles = _roles("raw/sample.wiff", "raw/sample.wiff2")

        self.assertEqual("raw_alternate", roles["raw/sample.wiff"])
        self.assertEqual("raw", roles["raw/sample.wiff2"])

    def test_a_wiff_without_a_wiff2_is_still_the_input(self) -> None:
        self.assertEqual("raw", _roles("raw/sample.wiff")["raw/sample.wiff"])

    def test_different_samples_are_never_paired(self) -> None:
        roles = _roles("raw/sample_01.d", "raw/sample_02.mzML")

        self.assertEqual({"raw"}, set(roles.values()))

    def test_a_sidecar_is_not_a_second_encoding(self) -> None:
        """A .wiff.scan belongs to its .wiff and is never an input in its own right."""
        roles = _roles("raw/sample.wiff", "raw/sample.wiff.scan")

        self.assertEqual("raw", roles["raw/sample.wiff"])
        self.assertEqual("sidecar", roles["raw/sample.wiff.scan"])


class SuffixTableTests(unittest.TestCase):
    def test_the_two_families_do_not_overlap(self) -> None:
        """A suffix in both lists would make the preference depend on iteration order."""
        self.assertEqual(set(), set(VENDOR_RAW_SUFFIXES) & set(CONVERTED_SUFFIXES))

    def test_wiff2_is_listed_as_vendor_so_it_is_never_demoted_to_an_mzml(self) -> None:
        self.assertIn(".wiff2", VENDOR_RAW_SUFFIXES)
        roles = _roles("raw/sample.wiff2", "raw/sample.mzML")
        self.assertEqual("raw", roles["raw/sample.wiff2"])
        self.assertEqual("raw_alternate", roles["raw/sample.mzML"])

    def test_the_lists_hold_only_what_msdial_opens(self) -> None:
        """SupportMsRawDataExtension: abf, ibf, cdf, mzml, wiff, raw, d, wiff2, qgd, lcd, lrp, imzml."""
        supported = {
            ".abf", ".ibf", ".cdf", ".mzml", ".wiff", ".raw", ".d", ".wiff2",
            ".qgd", ".lcd", ".lrp", ".imzml",
        }
        self.assertTrue(set(VENDOR_RAW_SUFFIXES + CONVERTED_SUFFIXES) <= supported)
        self.assertEqual(set(), set(UNREADABLE_SUFFIXES) & supported)


class NoMzxmlReaderTests(unittest.TestCase):
    """MS-DIAL has no mzXML parser, confirmed against SupportFormat.cs and by its author.

    Eight campaign-eligible units hold mzXML and nothing else. Treating it as an input would queue
    a run that cannot start, and the failure would surface inside MS-DIAL rather than before it.
    Since 2026-09-30 the campaign converts mzXML-only data to mzML, so an mzXML is marked for that
    conversion; which converter does it is the execution layer's choice, not the Catalog's.
    """

    def test_mzxml_is_not_a_readable_format(self) -> None:
        self.assertIn(".mzxml", UNREADABLE_SUFFIXES)
        self.assertNotIn(".mzxml", VENDOR_RAW_SUFFIXES + CONVERTED_SUFFIXES)

    def test_an_mzxml_is_marked_wherever_it_appears(self) -> None:
        """Alone, it stays the unit's only file and says what has to happen to it."""
        item = _files("raw/only.mzXML")[0]

        self.assertIn("converted to mzML", item["requires_conversion"])
        self.assertEqual("mzML", item["conversion_target"])
        self.assertNotIn("msconvert", item["requires_conversion"], "the converter is not the Catalog's")

    def test_only_mzxml_is_marked_convertible(self) -> None:
        """mzData and the rest have no planned route to a format MS-DIAL reads."""
        item = _files("raw/only.mzData")[0]

        self.assertTrue(item["requires_conversion"])
        self.assertNotIn("conversion_target", item)

    def test_an_mzxml_loses_to_a_vendor_container(self) -> None:
        """THE MTBKS157 SHAPE: sixteen samples published as .RAW and again as .mzXML."""
        roles = {f["path"]: f["role"] for f in _files("raw/s.RAW", "raw/s.mzXML")}

        self.assertEqual("raw", roles["raw/s.RAW"])
        self.assertEqual("raw_alternate", roles["raw/s.mzXML"])

    def test_an_mzxml_loses_to_an_mzml_of_the_same_sample(self) -> None:
        """Here the converted file wins, because the other converted file cannot be read at all."""
        result = _files("raw/s.mzML", "raw/s.mzXML")
        roles = {f["path"]: f["role"] for f in result}

        self.assertEqual("raw", roles["raw/s.mzML"])
        self.assertEqual("raw_alternate", roles["raw/s.mzXML"])
        demoted = next(f for f in result if f["role"] == "raw_alternate")
        self.assertIn("cannot read this format", demoted["demoted_because"])


class MzxmlOutranksAFormatWithNoRouteTests(unittest.TestCase):
    """Decided by the user on 2026-09-30: a convertible mzXML outranks an unreadable twin.

    MetaboLights MTBLS688 lists most of its samples twice, as x.mzXML.lzma under DERIVED_FILES and
    as x.dat under RAW_FILES, and both were inputs: 4,526 for the 2,263 samples of its negative
    unit. The mzXML is converted to mzML and analysed; the .dat, which MS-DIAL cannot read and
    nothing converts, is kept for provenance as the alternate.
    """

    NEG = "FILES/DERIVED_FILES/NEG1"
    RAW = "FILES/RAW_FILES/NEG1"

    def test_the_mtbls688_shape(self) -> None:
        result = {item["path"]: item for item in _files(f"{self.NEG}/s_Seg1Ev2.mzXML.lzma", f"{self.RAW}/s_Seg1Ev2.dat")}
        packed, dat = result[f"{self.NEG}/s_Seg1Ev2.mzXML.lzma"], result[f"{self.RAW}/s_Seg1Ev2.dat"]

        self.assertEqual(("raw", "raw_alternate"), (packed["role"], dat["role"]))
        self.assertEqual("mzML", packed["conversion_target"])
        self.assertIn("no conversion of it is planned", dat["demoted_because"])
        self.assertNotIn("conversion_target", dat, "still marked, but with nowhere to convert to")
        self.assertTrue(dat["requires_conversion"])

    def test_every_format_nothing_converts_loses_to_an_mzxml(self) -> None:
        for suffix in sorted(set(UNREADABLE_SUFFIXES) - set(CONVERTIBLE_SUFFIXES)):
            with self.subTest(suffix):
                roles = _roles("raw/s.mzXML", f"raw/s{suffix}")
                self.assertEqual({"raw/s.mzXML": "raw", f"raw/s{suffix}": "raw_alternate"}, roles)

    def test_it_never_demotes_a_container_msdial_reads(self) -> None:
        """Only where no vendor or converted container competes; they still win over both."""
        for suffix in VENDOR_RAW_SUFFIXES + CONVERTED_SUFFIXES:
            with self.subTest(suffix):
                roles = _roles(f"raw/s{suffix}", "raw/s.mzXML.lzma", "raw/s.dat")
                self.assertEqual("raw", roles[f"raw/s{suffix}"])
                self.assertEqual("raw_alternate", roles["raw/s.mzXML.lzma"])
                self.assertEqual("raw_alternate", roles["raw/s.dat"])

    def test_two_formats_nothing_converts_are_left_alone(self) -> None:
        self.assertEqual({"raw"}, set(_roles("raw/s.dat", "raw/s.mzData").values()))

    def test_the_row_naming_the_dat_names_the_mzxml_sample(self) -> None:
        """MTBLS688's rows name the .dat. Its sample, and its Factor Values, go to the mzXML."""
        unit = {
            "samples": [{"sample_id": "1-1_1", "raw_file": f"{self.RAW}/1-1_1_Seg1Ev2.dat",
                         "attributes": {"Factor Value[dose]": "1k5"}}],
            "files": [
                {"path": f"{self.NEG}/1-1_1_Seg1Ev2.mzXML.lzma", "role": "raw", "sample_id": ""},
                {"path": f"{self.RAW}/1-1_1_Seg1Ev2.dat", "role": "raw", "sample_id": ""},
            ],
        }

        view = normalize_analysis_unit(unit)
        (entry,) = view["analysis_inputs"]
        (sample,) = view["samples"]

        self.assertEqual((f"{self.NEG}/1-1_1_Seg1Ev2.mzXML", "1-1_1"), (entry["path"], entry["sample_id"]))
        self.assertEqual({"Factor Value[dose]": "1k5"}, sample["attributes"])
        self.assertEqual(f"{self.NEG}/1-1_1_Seg1Ev2.mzXML.lzma", sample["raw_file"], "named for what is analysed")
        again = normalize_analysis_unit(view)
        self.assertEqual(view["analysis_inputs"], again["analysis_inputs"], "the projection of a projection")
        self.assertEqual(view["samples"], again["samples"])

    def test_a_row_naming_the_input_itself_comes_first(self) -> None:
        unit = {
            "samples": [
                {"sample_id": "dat_row", "raw_file": "raw/s.dat"},
                {"sample_id": "mzxml_row", "raw_file": "raw/s.mzXML"},
            ],
            "files": [{"path": "raw/s.dat", "role": "raw"}, {"path": "raw/s.mzXML", "role": "raw"}],
        }

        (entry,) = normalize_analysis_unit(unit)["analysis_inputs"]

        self.assertEqual("mzxml_row", entry["sample_id"])

    def test_two_encodings_left_take_no_row_by_their_twin(self) -> None:
        """Which of two mzXML the .dat row meant is not stated, so neither is given it."""
        unit = {
            "samples": [{"sample_id": "s_row", "raw_file": "raw/s.dat"}],
            "files": [
                {"path": "raw/s.mzXML.gz", "role": "raw"},
                {"path": "raw/s.mzXML.lzma", "role": "raw"},
                {"path": "raw/s.dat", "role": "raw"},
            ],
        }

        view = normalize_analysis_unit(unit)

        self.assertEqual(2, view["analysis_file_count"])
        self.assertNotIn("s_row", [entry["sample_id"] for entry in view["analysis_inputs"]])

    def test_names_that_differ_are_not_paired(self) -> None:
        """MTBLS688 spells 105 samples of each unit two ways, 10plus_ and 10_plus__: not one stem."""
        unit = {
            "samples": [{"sample_id": "10+_1k5_10ul_1", "raw_file": f"{self.RAW}/10_plus__1k5_10ul_1_Seg1Ev2.dat"}],
            "files": [
                {"path": f"{self.NEG}/10plus_1k5_10ul_1_Seg1Ev2.mzXML.lzma", "role": "raw"},
                {"path": f"{self.RAW}/10_plus__1k5_10ul_1_Seg1Ev2.dat", "role": "raw"},
            ],
        }

        view = normalize_analysis_unit(unit)
        by_path = {entry["path"]: entry["sample_id"] for entry in view["analysis_inputs"]}

        self.assertEqual(2, len(by_path), "both stay inputs; pairing them would be an inference")
        self.assertEqual("10+_1k5_10ul_1", by_path[f"{self.RAW}/10_plus__1k5_10ul_1_Seg1Ev2.dat"])


class SharedVectorTests(unittest.TestCase):
    """The rule Interactive applies to files an archive held is this one; the vectors are shared."""

    def test_every_vector(self) -> None:
        document = json.loads(VECTORS.read_text(encoding="utf-8"))
        self.assertEqual("msdial-encoding-preference-vectors.v1", document["schema"])
        for case in document["cases"]:
            with self.subTest(case["name"]):
                result = {item["path"]: item for item in _files(*case["files"])}
                self.assertEqual(case["roles"], {path: item["role"] for path, item in result.items()})
                self.assertEqual(
                    sorted(case.get("requires_conversion", [])),
                    sorted(path for path, item in result.items() if item.get("requires_conversion")),
                )
                if "conversion_target" in case:
                    self.assertEqual(
                        case["conversion_target"],
                        {path: item["conversion_target"] for path, item in result.items() if "conversion_target" in item},
                    )
                if "unpacks_to" in case:
                    self.assertEqual(
                        case["unpacks_to"],
                        {path: item["unpacks_to"] for path, item in result.items() if "unpacks_to" in item},
                    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

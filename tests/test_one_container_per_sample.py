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

import unittest

from msdial_repository_catalog.class_proposal import (
    CONVERTED_SUFFIXES,
    VENDOR_RAW_SUFFIXES,
    normalize_file_roles,
)


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
        for vendor in (".d", ".raw", ".lcd", ".wiff", ".cdf"):
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
    def test_two_converted_encodings_are_left_alone(self) -> None:
        """This rule is about vendor versus converted. It has no view on mzML versus mzXML.

        Choosing between them would be a preference nobody has stated, and inventing one is how
        an unevidenced decision enters a pipeline.
        """
        roles = _roles("raw/both_open.mzML", "raw/both_open.mzXML")

        self.assertEqual({"raw"}, set(roles.values()))

    def test_two_vendor_containers_are_left_to_the_rule_that_knows_them(self) -> None:
        """.wiff versus .wiff2 is decided earlier, by _file_role, and this must not undo it."""
        roles = _roles("raw/sample.wiff", "raw/sample.wiff2")

        self.assertEqual("raw", roles["raw/sample.wiff"])
        self.assertEqual("raw_alternate", roles["raw/sample.wiff2"])

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
        """ZT Scan DIA reads the .wiff2, and a converted file must not outrank it."""
        self.assertIn(".wiff2", VENDOR_RAW_SUFFIXES)
        roles = _roles("raw/sample.wiff2", "raw/sample.mzML")
        self.assertEqual("raw", roles["raw/sample.wiff2"])
        self.assertEqual("raw_alternate", roles["raw/sample.mzML"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

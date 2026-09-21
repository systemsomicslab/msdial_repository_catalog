"""A study's samples must be split between its analysis units, not copied onto each of them.

Metabolomics Workbench publishes its factor table at study level, so the adapter computed the
sample list once and gave the same list to every analysis unit. It said so, in a warning written
onto every such unit: "Metabolomics Workbench factors are study-level; verify that each sample
belongs to this analysis_id." Nothing ever verified it, and nothing ever read the warning - a grep
of the whole execution layer for that sentence returns nothing.

Measured on 2026-09-21: of 536 studies holding a campaign-eligible LC-MS unit, 291 had units whose
file lists were identical. ST003038's "DDA Positive" and "DDA Negative" units each held the same
twenty files, ten named POS and ten named NEG. Running either would have aligned both polarities
together and called the result one ion mode.

The evidence to split them was present and unused: every sample carries a raw_file name and those
names state the polarity. This is the same evidence `_partition_archives` already uses for archive
names, applied to the samples.
"""

from __future__ import annotations

import unittest

from msdial_repository_catalog.adapters.workbench import _partition_samples, file_polarity


def _samples(*names: str) -> list[dict]:
    return [{"sample_id": name, "raw_file": name, "values": {}} for name in names]


class FilePolarityTests(unittest.TestCase):
    def test_the_shapes_the_catalog_actually_holds(self) -> None:
        self.assertEqual("Negative", file_polarity("211210_SVC_Pozzi__Lipidomics_NEG_S01.mzXML"))
        self.assertEqual("Positive", file_polarity("211210_SVC_Pozzi__Lipidomics_POS_S01.mzXML"))
        self.assertEqual("Negative", file_polarity("NEG_Ig04GST_LV10.raw"))
        self.assertEqual("Positive", file_polarity("POS_Ig04GST_LV10.raw"))
        self.assertEqual("Negative", file_polarity("M3T-Std_neg_DIA_20mz.mzML"))

    def test_a_surname_is_not_an_ion_mode(self) -> None:
        """A substring test reads "pos" out of words that have nothing to do with polarity.

        One real study in this catalog is submitted by Pozzi. Another word that would trip a
        substring test is "deposit", and "exposure" carries "pos" as well.
        """
        self.assertEqual("", file_polarity("Pozzi_sample_01.mzML"))
        self.assertEqual("", file_polarity("exposure_deposit_01.mzML"))

    def test_a_name_stating_both_states_neither(self) -> None:
        """A file named for both cannot be attributed to one, and guessing would be inventing."""
        self.assertEqual("", file_polarity("switching_POS_NEG_01.mzML"))

    def test_a_name_stating_nothing(self) -> None:
        self.assertEqual("", file_polarity("120721ElimC12HA1b"))
        self.assertEqual("", file_polarity(""))


class PartitionTests(unittest.TestCase):
    def test_a_study_holding_both_polarities_is_split(self) -> None:
        """THE FIX, on the real shape of ST003038."""
        samples = _samples(
            *[f"211210_SVC_Pozzi__Lipidomics_NEG_S{i:02d}.mzXML" for i in range(1, 11)],
            *[f"211210_SVC_Pozzi__Lipidomics_POS_S{i:02d}.mzXML" for i in range(1, 11)],
        )

        negative, reason = _partition_samples(samples, "Negative", multiple=True)
        positive, _ = _partition_samples(samples, "Positive", multiple=True)

        self.assertEqual("", reason)
        self.assertEqual(10, len(negative))
        self.assertEqual(10, len(positive))
        self.assertTrue(all("NEG" in item["raw_file"] for item in negative))
        self.assertTrue(all("POS" in item["raw_file"] for item in positive))
        self.assertEqual(set(), {i["sample_id"] for i in negative} & {i["sample_id"] for i in positive})

    def test_a_single_unit_study_is_left_alone(self) -> None:
        """With one analysis unit there is nothing to split it from."""
        samples = _samples("NEG_01.raw", "POS_01.raw")

        result, reason = _partition_samples(samples, "Negative", multiple=False)

        self.assertEqual(2, len(result))
        self.assertEqual("", reason)

    def test_names_that_state_no_polarity_are_not_split_on(self) -> None:
        """ST003399's raw_file values carry no extension and no polarity token.

        Returning an empty list for such a unit would be worse than returning all of them: it
        would silently delete a study from the campaign.
        """
        samples = _samples("120721ElimC12HA1b", "120721ElimC12HA2b", "120721ElimC12HB1b")

        result, reason = _partition_samples(samples, "Negative", multiple=True)

        self.assertEqual(3, len(result))
        self.assertIn("state no ion mode", reason)

    def test_a_partly_labelled_study_refuses_rather_than_dropping_the_rest(self) -> None:
        """Splitting on a partial signal would quietly discard every file that said nothing."""
        samples = _samples("NEG_01.raw", "POS_01.raw", "blank_01.raw", "qc_02.raw")

        result, reason = _partition_samples(samples, "Negative", multiple=True)

        self.assertEqual(4, len(result), "all of them, rather than a confident half")
        self.assertIn("would silently drop them", reason)

    def test_a_unit_whose_label_contradicts_every_file_is_reported(self) -> None:
        """Every file says Positive and the unit says Negative. One of them is wrong."""
        samples = _samples("POS_01.raw", "POS_02.raw")

        result, reason = _partition_samples(samples, "Negative", multiple=True)

        self.assertEqual(2, len(result))
        self.assertIn("every raw-file name states Positive", reason)

    def test_a_single_polarity_study_that_agrees_needs_no_split(self) -> None:
        samples = _samples("NEG_01.raw", "NEG_02.raw")

        result, reason = _partition_samples(samples, "Negative", multiple=True)

        self.assertEqual(2, len(result))
        self.assertEqual("", reason)

    def test_a_unit_labelled_both_is_not_split(self) -> None:
        """A unit that declares both polarities is asking for both, whatever the files say."""
        samples = _samples("NEG_01.raw", "POS_01.raw")

        result, reason = _partition_samples(samples, "Both", multiple=True)

        self.assertEqual(2, len(result))
        self.assertEqual("", reason)

    def test_a_unit_of_unknown_polarity_is_not_split(self) -> None:
        samples = _samples("NEG_01.raw", "POS_01.raw")

        result, reason = _partition_samples(samples, "Unknown", multiple=True)

        self.assertEqual(2, len(result))
        self.assertEqual("", reason)

    def test_the_split_never_returns_nothing(self) -> None:
        """An empty unit is the one outcome that must never come out of a split.

        Every refusal path returns the whole list with a reason, because a unit with no samples
        looks exactly like a study that was never deposited.
        """
        for ion_mode in ("Positive", "Negative", "Both", "Unknown"):
            for names in (("a.raw", "b.raw"), ("NEG_1.raw",), ("POS_1.raw", "x.raw")):
                result, _ = _partition_samples(_samples(*names), ion_mode, multiple=True)
                self.assertTrue(result, f"{ion_mode} {names} produced an empty unit")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

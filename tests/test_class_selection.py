"""Class is chosen from what the submitter declared, or not chosen at all.

An agent reanalysing a whole repository cannot ask anyone which column is the design, and
until now nothing chose one: `field_based_proposal` takes `selected_fields` from its caller,
and the only ranking in the project - `candidate_fields` - scores whatever columns exist, so
`Factor Value[Batch]` scores above nothing and a technical column can define the comparison.

The rule these tests pin is that a declaration is the evidence. `Factor Value[Treatment]` is
a submitter stating what the experiment varied; `Characteristics[Genotype]`, a comment, or an
abstract is not, however well it happens to group the samples. When no declaration supports a
contrast the selector abstains, which is a result and not a failure - MS-DIAL never reads
Class during peak detection, alignment or annotation - and the reason is recorded so a
researcher can overrule it after the run.
"""

from __future__ import annotations

import unittest

from msdial_repository_catalog.class_selection import (
    automatic_class_proposal,
    declared_factor_name,
    select_class_fields,
)


def _unit(attributes_per_sample: list[dict[str, str]], unit_id: str = "unit-1") -> dict:
    return {
        "unit_id": unit_id,
        "files": [],
        "samples": [
            {"sample_id": f"S{index + 1}", "raw_file": f"S{index + 1}.mzML", "attributes": attributes}
            for index, attributes in enumerate(attributes_per_sample)
        ],
    }


def _repeat(field: str, values: list[str], times: int = 1) -> list[dict[str, str]]:
    return [{field: value} for value in values for _ in range(times)]


class OnlyADeclaredFactorCanDefineClass(unittest.TestCase):
    def test_a_declared_factor_is_adopted_and_named_in_the_record(self) -> None:
        unit = _unit(_repeat("Factor Value[Treatment]", ["control", "dosed"], times=4))

        decision = select_class_fields(unit, "compare dosed against control")

        self.assertEqual("declared", decision["decision"])
        self.assertEqual(["Factor Value[Treatment]"], decision["selected_fields"])
        self.assertIn("Factor Value[Treatment]", decision["rationale"])
        self.assertIn("compare dosed against control", decision["rationale"])

    def test_an_undeclared_column_is_never_adopted_however_well_it_groups(self) -> None:
        """THE RULE. This column splits the samples perfectly and still means nothing.

        A Characteristics column records a property of the material. It does not state that
        the experiment varied it, and a reanalysis that treats it as the design reports a
        comparison the study never made.
        """
        unit = _unit(_repeat("Characteristics[Genotype]", ["wild type", "knockout"], times=4))

        decision = select_class_fields(unit, "")

        self.assertEqual("abstained", decision["decision"])
        self.assertEqual("no_declared_factor", decision["reason"])
        self.assertEqual([], decision["selected_fields"])
        self.assertEqual(1, decision["undeclared_columns"])

    def test_a_column_that_merely_contains_the_word_factor_is_not_a_declaration(self) -> None:
        """Tissue Factor is a coagulation protein and Dilution factor is a bench number.

        A substring test adopts either one as a study design, which is why the shapes are
        anchored rather than searched.
        """
        for field in ("Dilution factor", "Tissue Factor", "Treatment Factor", "diluted factor"):
            self.assertIsNone(declared_factor_name(field), field)

    def test_the_declaration_shapes_the_repositories_actually_use(self) -> None:
        self.assertEqual("Treatment", declared_factor_name("Factor Value[Treatment]"))
        self.assertEqual("Gender", declared_factor_name("Factor Value [Gender]"))
        self.assertEqual("Group", declared_factor_name("Factor[Group]"))
        self.assertEqual("bacteria", declared_factor_name("Factor 2 (bacteria)"))
        self.assertEqual("Infection", declared_factor_name("Infection (Factor)"))
        self.assertEqual("Group", declared_factor_name("Factor Group"))

    def test_a_bare_factor_column_is_declared_without_being_named(self) -> None:
        """The Metabolomics Workbench shape: the column is called Factor and holds the level.

        "" says declared-and-unnamed, which is a different answer from None, and it must not
        fall through the vocabulary into nuisance or covariate.
        """
        self.assertEqual("", declared_factor_name("Factor"))
        self.assertEqual("", declared_factor_name("FACTORS"))
        self.assertEqual("", declared_factor_name("Factor1"))

        decision = select_class_fields(_unit(_repeat("Factor", ["fed", "fasted"], times=4)), "")

        self.assertEqual(["Factor"], decision["selected_fields"])


class ADeclarationIsNotEnoughByItself(unittest.TestCase):
    def test_a_declared_nuisance_factor_is_refused_even_when_perfectly_shaped(self) -> None:
        """Batch groups the samples cleanly and reports the laboratory, not the biology."""
        unit = _unit(_repeat("Factor Value[Batch]", ["batch 1", "batch 2"], times=5))

        decision = select_class_fields(unit, "")

        self.assertEqual("abstained", decision["decision"])
        self.assertEqual("no_usable_declared_factor", decision["reason"])
        refused = decision["considered"][0]
        self.assertEqual("nuisance", refused["verdict"])
        self.assertIn("how the samples were handled", refused["reason"])

    def test_every_nuisance_name_the_catalog_actually_carries(self) -> None:
        for name in ("Replicate", "Batch", "Injection number", "Analysis date", "Plate effect",
                     "Biological replicate", "Spectrum type", "Run order"):
            unit = _unit(_repeat(f"Factor Value[{name}]", ["a", "b"], times=4))
            self.assertEqual("abstained", select_class_fields(unit, "")["decision"], name)

    def test_an_analytical_method_declared_as_a_factor_is_still_analytical(self) -> None:
        """Found in MetaboLights MTBLS1572, surveying the campaign-eligible pool.

        That study declares Factor Value[Data acquisition mode] with the levels DDA, DIA and
        Full-scan, seven files each. It is a genuine declaration and a genuine comparison -
        the study compares acquisition methods - but it is a comparison of instruments, not
        of biology, and a run grouped by it reports the method as the finding. The unit also
        has to be split before it can run at all, because the campaign accepts one acquisition
        mode per MS-DIAL run.
        """
        unit = _unit(_repeat("Factor Value[Data acquisition mode]", ["DDA", "DIA", "Full-scan"], times=7))

        decision = select_class_fields(unit, "")

        self.assertEqual("abstained", decision["decision"])
        self.assertEqual("nuisance", decision["considered"][0]["verdict"])

    def test_a_single_level_column_defines_no_comparison(self) -> None:
        unit = _unit(_repeat("Factor Value[Treatment]", ["control"], times=8))

        decision = select_class_fields(unit, "")

        self.assertEqual("abstained", decision["decision"])
        self.assertEqual("no_contrast", decision["considered"][0]["verdict"])

    def test_a_value_per_sample_identifies_rather_than_groups(self) -> None:
        unit = _unit(_repeat("Factor Value[Genotype]", [f"line {index}" for index in range(12)]))

        decision = select_class_fields(unit, "")

        self.assertEqual("abstained", decision["decision"])
        self.assertEqual("identifier", decision["considered"][0]["verdict"])

    def test_a_mostly_empty_column_would_group_most_samples_as_nothing(self) -> None:
        attributes = [{"Factor Value[Treatment]": "dosed"} for _ in range(2)]
        attributes += [{"Factor Value[Treatment]": "control"} for _ in range(2)]
        attributes += [{"Factor Value[Treatment]": ""} for _ in range(6)]

        decision = select_class_fields(_unit(attributes), "")

        self.assertEqual("abstained", decision["decision"])
        self.assertEqual("too_sparse", decision["considered"][0]["verdict"])

    def test_a_replicated_panel_is_a_design_and_a_continuous_score_is_not(self) -> None:
        """Both make many groups; only one of them grouped anything.

        Measured in the catalog: Factor Value[Cultivar] gives 129 levels of five samples each
        over 680 samples, which is a germplasm panel and a real design. Factor Value[Frailty
        Index Score] gives 702 levels over 1,551 samples, most holding one sample, which is a
        measurement per subject - the continuous field the project contract says not to group
        by. A level count alone cannot tell them apart; replication can.
        """
        panel = [{"Factor Value[Cultivar]": f"cultivar {index}"} for index in range(20) for _ in range(5)]
        score = [{"Factor Value[Frailty Index Score]": f"{index * 0.017:.3f}"} for index in range(100)]

        self.assertEqual("declared", select_class_fields(_unit(panel), "")["decision"])

        refused = select_class_fields(_unit(score), "")
        self.assertEqual("abstained", refused["decision"])
        self.assertIn(
            refused["considered"][0]["verdict"], {"unreplicated", "identifier", "too_granular"}
        )

    def test_unknown_is_an_absence_and_not_a_group(self) -> None:
        """MS-DIAL would otherwise compare the unknowns against the treated animals.

        "unknown" is the single most common value in the catalog's bare Factor column, so a
        selector that counts it as a level adopts columns that say nothing.
        """
        attributes = [{"Factor": "dosed"} for _ in range(2)]
        attributes += [{"Factor": "control"} for _ in range(2)]
        attributes += [{"Factor": "unknown"} for _ in range(6)]

        decision = select_class_fields(_unit(attributes), "")

        self.assertEqual("abstained", decision["decision"])
        self.assertEqual("too_sparse", decision["considered"][0]["verdict"])
        self.assertEqual(4, decision["considered"][0]["present"])
        self.assertEqual(2, decision["considered"][0]["distinct_count"], "unknown is not a level")


class ACovariateIsTheLastResortAndSaysSo(unittest.TestCase):
    def test_a_designed_contrast_outranks_a_subject_covariate(self) -> None:
        attributes = [
            {"Factor Value[Treatment]": treatment, "Factor Value[Gender]": gender}
            for treatment in ("control", "dosed")
            for gender in ("male", "female")
            for _ in range(3)
        ]

        decision = select_class_fields(_unit(attributes), "")

        self.assertEqual("Factor Value[Treatment]", decision["selected_fields"][0])

    def test_a_covariate_alone_is_used_and_flagged_as_a_covariate(self) -> None:
        """Sex may well be the design, and the catalog cannot know that it is.

        Using it is more useful than abstaining, and saying it is a covariate is what lets a
        researcher see in one line that nobody designed this comparison.
        """
        unit = _unit(_repeat("Factor Value[Gender]", ["male", "female"], times=4))

        decision = select_class_fields(unit, "")

        self.assertEqual("declared", decision["decision"])
        self.assertEqual(["Factor Value[Gender]"], decision["selected_fields"])
        self.assertIn("covariate rather than by a designed contrast", decision["rationale"])
        self.assertTrue(any("subject covariate" in item for item in decision["warnings"]))


class CrossingTwoFactorsNeedsTheDesignToSupportIt(unittest.TestCase):
    def test_a_balanced_two_by_two_is_crossed(self) -> None:
        attributes = [
            {"Factor Value[Treatment]": treatment, "Factor Value[Timepoint]": time}
            for treatment in ("control", "dosed")
            for time in ("0h", "24h")
            for _ in range(3)
        ]

        decision = select_class_fields(_unit(attributes), "")

        self.assertEqual(
            ["Factor Value[Treatment]", "Factor Value[Timepoint]"], decision["selected_fields"]
        )
        self.assertIn("crossed", decision["rationale"])

    def test_an_incidental_pairing_is_not_crossed_into_singleton_groups(self) -> None:
        """Crossing can reach the identifier case by another route; one sample per cell is it."""
        attributes = [
            {"Factor Value[Treatment]": treatment, "Factor Value[Cohort]": f"cohort {index}"}
            for index, treatment in enumerate(["control", "dosed"] * 4)
        ]

        decision = select_class_fields(_unit(attributes), "")

        self.assertEqual(["Factor Value[Treatment]"], decision["selected_fields"])


class TheDecisionIsRecordedWhicheverWayItGoes(unittest.TestCase):
    def test_an_abstention_returns_no_proposal_and_a_reason(self) -> None:
        proposal, decision = automatic_class_proposal(_unit([{"sample source": "plasma"}] * 6), "")

        self.assertIsNone(proposal)
        self.assertEqual("no_declared_factor", decision["reason"])
        self.assertIn("declared no experimental factor", decision["notice"])

    def test_the_notice_reaches_the_proposal_the_catalog_persists(self) -> None:
        """The user's condition for letting an agent choose: the notification must survive.

        class_proposal.warnings is stored in warnings_json, so a researcher opening the unit
        later reads that the catalog chose this grouping and that nobody checked it.
        """
        unit = _unit(_repeat("Factor Value[Treatment]", ["control", "dosed"], times=4))

        proposal, _ = automatic_class_proposal(unit, "compare dosed against control")

        assert proposal is not None
        self.assertIn("without a person reading the study", proposal.warnings[0])
        self.assertEqual("catalog-declared-factor-selection", proposal.model)
        self.assertEqual(8, len(proposal.assignments))
        self.assertEqual({"control", "dosed"}, {item.class_label for item in proposal.assignments})

    def test_the_contrast_definition_states_the_levels_that_were_compared(self) -> None:
        unit = _unit(_repeat("Factor Value[Treatment]", ["control", "dosed"], times=4))

        proposal, _ = automatic_class_proposal(unit, "")

        assert proposal is not None
        self.assertEqual("declared_factor", proposal.contrast_definition["kind"])
        self.assertEqual(
            ["control", "dosed"], proposal.contrast_definition["levels"]["Factor Value[Treatment]"]
        )

    def test_a_small_group_is_reported_rather_than_presented_as_a_comparison(self) -> None:
        attributes = [{"Factor Value[Treatment]": "control"} for _ in range(7)]
        attributes += [{"Factor Value[Treatment]": "dosed"} for _ in range(2)]

        decision = select_class_fields(_unit(attributes), "")

        self.assertEqual("declared", decision["decision"])
        self.assertTrue(any("descriptive, not statistical" in item for item in decision["warnings"]))

    def test_sample_type_values_are_reported_rather_than_merged(self) -> None:
        attributes = [{"Factor": "control"} for _ in range(4)]
        attributes += [{"Factor": "dosed"} for _ in range(4)]
        attributes += [{"Factor": "QC"} for _ in range(3)]

        decision = select_class_fields(_unit(attributes), "")

        self.assertEqual("declared", decision["decision"])
        self.assertTrue(any("sample-type values" in item for item in decision["warnings"]))

    def test_the_placeholder_the_submitter_wrote_is_named_in_the_warning(self) -> None:
        """The label is the submitter's own word, so the warning must not claim it says NA."""
        attributes = [{"Factor Value[Treatment]": "control"} for _ in range(4)]
        attributes += [{"Factor Value[Treatment]": "dosed"} for _ in range(4)]
        attributes += [{"Factor Value[Treatment]": "unknown"} for _ in range(2)]

        decision = select_class_fields(_unit(attributes), "")

        self.assertEqual("declared", decision["decision"])
        self.assertTrue(
            any("unknown" in item and "own Class" in item for item in decision["warnings"]),
            decision["warnings"],
        )

    def test_a_missing_purpose_is_recorded_rather_than_assumed_away(self) -> None:
        unit = _unit(_repeat("Factor Value[Treatment]", ["control", "dosed"], times=4))

        decision = select_class_fields(unit, "")

        self.assertTrue(any("No analysis purpose was stated" in item for item in decision["warnings"]))

    def test_refused_factors_are_listed_beside_the_one_that_was_used(self) -> None:
        attributes = [
            {
                "Factor Value[Treatment]": treatment,
                "Factor Value[Batch]": "batch 1",
                "Factor Value[Replicate]": str(index),
            }
            for index, treatment in enumerate(["control", "dosed"] * 4)
        ]

        decision = select_class_fields(_unit(attributes), "")

        self.assertEqual(["Factor Value[Treatment]"], decision["selected_fields"])
        listed = " ".join(decision["warnings"])
        self.assertIn("Factor Value[Batch]", listed)
        self.assertIn("Factor Value[Replicate]", listed)
        self.assertIn("Factor Value[Batch]", decision["design_covariates"])


class TheSameUnitAlwaysGetsTheSameAnswer(unittest.TestCase):
    def test_the_order_the_columns_happen_to_arrive_in_changes_nothing(self) -> None:
        """An agent reruns a unit and must not get a different design than last time."""
        forward = [
            {"Factor Value[Treatment]": treatment, "Factor Value[Disease]": disease}
            for treatment in ("control", "dosed")
            for disease in ("healthy", "diabetic")
            for _ in range(3)
        ]
        reversed_columns = [
            {key: row[key] for key in reversed(list(row))} for row in reversed(forward)
        ]

        first = select_class_fields(_unit(forward), "")
        second = select_class_fields(_unit(reversed_columns), "")

        self.assertEqual(first["selected_fields"], second["selected_fields"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

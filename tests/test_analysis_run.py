"""analysis_run gets its writer: one row per production run, written the same way however often.

The table existed from the first schema with no writer anywhere. A campaign runner records a run
when it starts and again when the gate has read it, and a crash between the two means the second
write can come twice; the row must not care. What the row says also has limits: its paths are
relative to the unit's workspace, because these rows live beside the catalog and no absolute
location -- a private library's above all -- should travel with it, and "completed" means what the
trial manifest defines it to mean, a gate --strict exit 0, not "the Console exited".
"""

from __future__ import annotations

import gzip
import sqlite3
import tempfile
import unittest
from pathlib import Path

from msdial_repository_catalog.class_selection import abstention_record
from msdial_repository_catalog.mcp_server import msdial_catalog_record_analysis_run
from msdial_repository_catalog.normalize import project_to_study
from msdial_repository_catalog.storage import Catalog

from test_catalog import mixed_project


class RunRecordFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "catalog.sqlite"
        study = project_to_study(mixed_project())
        self.unit_id, self.other_unit_id = (unit.unit_id for unit in study.analysis_units)
        with Catalog(self.database) as catalog:
            catalog.ingest_study(study)
            record, _ = abstention_record(catalog.get_unit(self.unit_id), "Profile the lipidome")
            record.status = "accepted"
            catalog.save_class_proposal(record)
        self.proposal_id = record.proposal_id
        self.run_id = f"{self.unit_id}:job-0001"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def record(self, run_id: str | None = None, **values: object) -> dict:
        values.setdefault("unit_id", self.unit_id)
        values.setdefault("status", "running")
        with Catalog(self.database) as catalog:
            return catalog.record_analysis_run(run_id or self.run_id, **values)

    def rows(self) -> int:
        with Catalog(self.database) as catalog:
            return catalog.connection.execute("SELECT COUNT(*) FROM analysis_run").fetchone()[0]


class RecordingIsIdempotent(RunRecordFixture):
    def test_the_same_record_twice_is_one_row_that_did_not_change(self) -> None:
        values = dict(
            status="completed", class_proposal_id=self.proposal_id, msdial_version="5.5.250930",
            parameter_hash="sha256:" + "1" * 64, parameter_file="provenance\\method.txt",
            mztab_path="output/unit.mzTab", mztab_sha256="2" * 64, qa_status="warn",
            gate_verdict="pass", gate_exit_code=0, output_paths={"alignment": "output/alignment.txt"},
            started_at="2026-09-30T01:00:00+00:00", completed_at="2026-09-30T02:00:00+00:00",
            provenance={"console_commit": "c471463a5", "libraries": [{"name": "vs21-pos.msp", "sha256": "3" * 64}]},
        )

        first = self.record(**values)
        second = self.record(**values)

        self.assertEqual((True, True), (first["created"], first["written"]))
        self.assertEqual((False, False), (second["created"], second["written"]))
        self.assertEqual(first["run"], second["run"], "updated_at included")
        self.assertEqual(1, self.rows())
        run = second["run"]
        self.assertEqual(("completed", "pass", 0), (run["status"], run["gate_verdict"], run["gate_exit_code"]))
        self.assertEqual("provenance/method.txt", run["parameter_file"], "stored in one separator form")
        self.assertEqual({"alignment": "output/alignment.txt"}, run["output_paths"])
        self.assertEqual(3, run["catalog_schema_version"])
        self.assertEqual(run["recorded_at"], run["updated_at"])

    def test_a_later_call_completes_the_record_without_resending_it(self) -> None:
        self.record(status="running", started_at="2026-09-30T01:00:00+00:00",
                    output_paths={"workspace_manifest": "provenance/run-manifest.json"})

        result = self.record(status="outputs_produced_reading_pending", gate_verdict="held",
                             gate_exit_code=4, completed_at="2026-09-30T02:00:00+00:00",
                             output_paths={"mztab": "output/unit.mzTab"})

        run = result["run"]
        self.assertTrue(result["written"])
        self.assertEqual(1, self.rows())
        self.assertEqual("2026-09-30T01:00:00+00:00", run["started_at"], "kept from the first write")
        self.assertEqual(
            {"workspace_manifest": "provenance/run-manifest.json", "mztab": "output/unit.mzTab"},
            run["output_paths"],
        )
        self.assertEqual(4, run["gate_exit_code"])
        self.assertNotEqual(run["recorded_at"], "")
        self.assertGreaterEqual(run["updated_at"], run["recorded_at"])

    def test_completed_is_reserved_for_a_strict_gate_exit_zero(self) -> None:
        for code in (None, 2, 4):
            with self.subTest(gate_exit_code=code), self.assertRaises(ValueError):
                self.record(status="completed", gate_exit_code=code)
        self.assertEqual(0, self.rows())
        self.record(status="gate_checked", gate_exit_code=0)
        self.assertEqual("completed", self.record(status="completed")["run"]["status"],
                         "the exit code recorded earlier counts")

    def test_a_split_part_is_recorded_against_its_parent_unit(self) -> None:
        run = self.record(f"{self.unit_id}-dda:job-0002")["run"]

        self.assertEqual((f"{self.unit_id}-dda:job-0002", self.unit_id), (run["run_id"], run["unit_id"]))


class ARunRecordSaysOnlyWhatItMay(RunRecordFixture):
    def test_run_ids_name_their_unit_and_a_job(self) -> None:
        for run_id in (
            f"{self.other_unit_id}:job-1",   # another unit's run
            self.unit_id,                    # no production job id
            f"{self.unit_id}:",              # an empty one
            f"{self.unit_id}x:job-1",        # a unit id that only starts the same
            f"{self.unit_id}-:job-1",        # a part with no name
            f"{self.unit_id}:job 1",         # whitespace
        ):
            with self.subTest(run_id=run_id), self.assertRaises(ValueError):
                self.record(run_id)
        self.assertEqual(0, self.rows())

    def test_one_run_id_belongs_to_one_unit(self) -> None:
        """A part of unit U and a unit named U-dda would read the same run id; the first owner keeps it."""
        with Catalog(self.database) as catalog, catalog.connection:
            catalog.connection.execute(
                "INSERT INTO analysis_unit (unit_id, study_id, source_subrecord_id, signature) "
                "SELECT ?, study_id, 'look-alike', 'x' FROM analysis_unit WHERE unit_id = ?",
                (self.unit_id + "-dda", self.unit_id),
            )
        self.record(f"{self.unit_id}-dda:job-9")
        with self.assertRaises(ValueError):
            self.record(f"{self.unit_id}-dda:job-9", unit_id=self.unit_id + "-dda")

    def test_no_absolute_location_is_recorded(self) -> None:
        for values in (
            {"mztab_path": "D:\\13_MSDIAL_Public_Reanalysis\\analysis\\x.mzTab"},
            {"parameter_file": "/home/lab/method.txt"},
            {"output_paths": {"mztab": "../other-unit/output/x.mzTab"}},
            {"output_paths": {"mztab": "C:/x.mzTab"}},
            {"provenance": {"library": "D:\\private\\VS21_pos.msp"}},
            {"provenance": {"libraries": [{"uri": "file:///D:/private/VS21_pos.msp"}]}},
            {"provenance": {"share": "\\\\nas\\libraries\\VS21_pos.msp"}},
            {"msdial_version": "5.5 (D:/0_SourceCode/MsdialWorkbench)"},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.record(**values)
        self.assertEqual(0, self.rows())
        run = self.record(provenance={"source": "https://www.ebi.ac.uk/metabolights/MTBLS2207"})["run"]
        self.assertEqual("https://www.ebi.ac.uk/metabolights/MTBLS2207", run["provenance"]["source"],
                         "a public URL is not a local location")

    def test_the_proposal_must_be_the_units_own(self) -> None:
        with self.assertRaises(ValueError):
            self.record(f"{self.other_unit_id}:job-1", unit_id=self.other_unit_id,
                        class_proposal_id=self.proposal_id)
        with self.assertRaises(KeyError):
            self.record("not-a-unit:job-1", unit_id="not-a-unit")

    def test_the_mcp_tool_records_and_reports_refusals(self) -> None:
        recorded = msdial_catalog_record_analysis_run(
            self.run_id, self.unit_id, "failed", gate_verdict="fail", gate_exit_code=2,
            output_paths={"failure": "provenance/failure-record.json"}, database=str(self.database),
        )
        again = msdial_catalog_record_analysis_run(
            self.run_id, self.unit_id, "failed", gate_verdict="fail", gate_exit_code=2,
            output_paths={"failure": "provenance/failure-record.json"}, database=str(self.database),
        )
        refused = msdial_catalog_record_analysis_run(
            self.run_id, self.unit_id, "completed", database=str(self.database)
        )

        self.assertTrue(recorded["recorded"])
        self.assertEqual("failed", recorded["run"]["status"])
        self.assertTrue(again["recorded"])
        self.assertFalse(again["written"])
        self.assertFalse(refused["recorded"])
        self.assertIn("gate --strict exited 0", refused["message"])


class RunRecordsAreLocalDecisions(RunRecordFixture):
    def test_run_records_leave_a_snapshot_only_with_the_local_decisions(self) -> None:
        self.record()
        counts = []
        for include in (False, True):
            output = Path(self.directory.name) / f"snapshot-{include}.sqlite.gz"
            with Catalog(self.database) as catalog:
                catalog.snapshot(output, include_local_decisions=include)
            extracted = output.with_suffix("")
            extracted.write_bytes(gzip.decompress(output.read_bytes()))
            connection = sqlite3.connect(extracted)
            try:
                counts.append(connection.execute("SELECT COUNT(*) FROM analysis_run").fetchone()[0])
            finally:
                connection.close()

        self.assertEqual([0, 1], counts)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

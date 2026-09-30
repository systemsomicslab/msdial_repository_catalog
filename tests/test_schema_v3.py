"""Schema 3 is additive: a schema-2 catalog keeps every row and gains the ratification and run columns.

The live catalog is several gigabytes and holds the Class proposals people confirmed, so the
migration must add columns and touch nothing else. A proposal saved before schema 3 was confirmed in
a conversation, and nothing recorded which; it must read unratified, not ratified by nobody.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from msdial_repository_catalog.schema import BASE_SCHEMA, SCHEMA_VERSION
from msdial_repository_catalog.storage import Catalog

V3_COLUMNS = (
    "    ratified_by TEXT NOT NULL DEFAULT '',\n",
    "    ratification_json TEXT NOT NULL DEFAULT '{}'\n",
    "    status TEXT NOT NULL DEFAULT '',\n",
    "    gate_verdict TEXT NOT NULL DEFAULT '',\n",
    "    gate_exit_code INTEGER,\n",
    "    output_paths_json TEXT NOT NULL DEFAULT '{}',\n",
    "    recorded_at TEXT NOT NULL DEFAULT '',\n",
    "    updated_at TEXT NOT NULL DEFAULT ''\n",
)


def schema_two() -> str:
    """BASE_SCHEMA as schema 2 wrote it: without the eight schema-3 columns."""
    schema = BASE_SCHEMA
    for line in V3_COLUMNS:
        if line not in schema:
            raise AssertionError(f"the schema-3 column {line.strip()!r} moved; update this fixture")
        schema = schema.replace(line, "")
    # The columns before the removed ones lose their trailing comma with them.
    return (
        schema.replace("    created_at TEXT NOT NULL,\n);", "    created_at TEXT NOT NULL\n);")
        .replace("    provenance_json TEXT NOT NULL DEFAULT '{}',\n);", "    provenance_json TEXT NOT NULL DEFAULT '{}'\n);")
    )


def schema_two_fixture(database: Path) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.executescript(schema_two())
        connection.execute("INSERT INTO schema_info(version) VALUES (2)")
        connection.execute(
            "INSERT INTO study (study_id, repository, accession, source_hash) VALUES ('s', 'example', 'EX', 'x')"
        )
        connection.execute(
            "INSERT INTO analysis_unit (unit_id, study_id, source_subrecord_id, signature) VALUES ('u', 's', 'u', 'x')"
        )
        connection.execute("INSERT INTO sample (sample_pk, unit_id, sample_id) VALUES ('p', 'u', 'S1')")
        connection.execute(
            "INSERT INTO class_proposal VALUES ('prop', 'u', 'purpose', '[\"Group\"]', 'confirmed in chat', "
            "'{}', 'reviewer', '', 'accepted', '[]', '2026-09-01T00:00:00+00:00')"
        )
        connection.execute("INSERT INTO class_assignment VALUES ('prop', 'S1', 'A', '{}')")
        connection.execute(
            "INSERT INTO analysis_run (run_id, unit_id, class_proposal_id, msdial_version, "
            "catalog_schema_version, parameter_hash) VALUES ('u:job-0', 'u', 'prop', '5.5', 2, 'h')"
        )
        connection.commit()
    finally:
        connection.close()


class SchemaTwoMigratesToThree(unittest.TestCase):
    def test_the_fixture_is_really_schema_two(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "catalog.sqlite"
            schema_two_fixture(database)
            connection = sqlite3.connect(database)
            try:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(class_proposal)")}
            finally:
                connection.close()
        self.assertNotIn("ratified_by", columns)

    def test_migration_adds_the_columns_and_keeps_every_row(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "catalog.sqlite"
            schema_two_fixture(database)
            with Catalog(database) as catalog:
                catalog.initialize()
                version = catalog.connection.execute("SELECT version FROM schema_info").fetchone()[0]
                proposal_columns = {row[1] for row in catalog.connection.execute("PRAGMA table_info(class_proposal)")}
                run_columns = {row[1] for row in catalog.connection.execute("PRAGMA table_info(analysis_run)")}
                proposal = catalog.get_class_proposal("prop")
                run = catalog.get_analysis_run("u:job-0")
                catalog.initialize()  # A second start migrates nothing twice.
                again = catalog.connection.execute("SELECT version FROM schema_info").fetchone()[0]

        self.assertEqual(3, SCHEMA_VERSION)
        self.assertEqual((SCHEMA_VERSION, SCHEMA_VERSION), (version, again))
        self.assertTrue({"ratified_by", "ratification_json"} <= proposal_columns)
        self.assertTrue(
            {"status", "gate_verdict", "gate_exit_code", "output_paths_json", "recorded_at", "updated_at"}
            <= run_columns
        )
        self.assertEqual("accepted", proposal["status"])
        self.assertEqual("confirmed in chat", proposal["rationale"])
        self.assertEqual([{"sample_id": "S1", "class_label": "A", "values": {}}], proposal["assignments"])
        self.assertEqual("", proposal["ratified_by"])
        self.assertIsNone(proposal["ratification"], "confirmed in a conversation, so not ratified by anyone")
        self.assertEqual(("prop", "5.5", "", None, {}),
                         (run["class_proposal_id"], run["msdial_version"], run["status"],
                          run["gate_exit_code"], run["output_paths"]))

    def test_schema_one_migrates_through_two_to_three(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "catalog.sqlite"
            legacy = schema_two().replace("    current_snapshot_id TEXT NOT NULL DEFAULT '',\n", "").replace(
                "    source_blob_hash TEXT NOT NULL DEFAULT '',\n", ""
            )
            connection = sqlite3.connect(database)
            try:
                connection.executescript(legacy)
                connection.execute("INSERT INTO schema_info(version) VALUES (1)")
                connection.commit()
            finally:
                connection.close()
            with Catalog(database) as catalog:
                catalog.initialize()
                version = catalog.connection.execute("SELECT version FROM schema_info").fetchone()[0]
                study = {row[1] for row in catalog.connection.execute("PRAGMA table_info(study)")}
                proposal = {row[1] for row in catalog.connection.execute("PRAGMA table_info(class_proposal)")}

        self.assertEqual(SCHEMA_VERSION, version)
        self.assertIn("current_snapshot_id", study)
        self.assertIn("ratified_by", proposal)

    def test_a_newer_schema_is_still_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "catalog.sqlite"
            with Catalog(database) as catalog:
                catalog.initialize()
                with catalog.connection:
                    catalog.connection.execute("UPDATE schema_info SET version = ?", (SCHEMA_VERSION + 1,))
                with self.assertRaises(RuntimeError):
                    catalog.initialize()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

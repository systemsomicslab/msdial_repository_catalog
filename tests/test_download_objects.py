"""One object per URL, as a download store holds it, with every unit that needs it.

A campaign fetches each repository object once and links it into every unit that lists it. For that
the Catalog has to say, per URL: what the object is called once it lands, how large it is -- or that
nobody listed its size -- which checksum vouches for it, and which units claim it. The last is what
keeps a store from deleting a Workbench archive that the negative-mode unit of the same study has not
used yet, and the size is what a disk guard admits a unit on.

The shapes are the repositories' own:

  Workbench     one study archive per URL, listed by every unit of the study when the archives
                cannot be told apart by analysis or polarity (shared_raw_archive), with an MD5
  MB-POST       one URL per project serving every file in one tar, which Interactive names
                <accession>.tar; the MD5s are the members', not the tar's
  MetaboLights  one URL per file, often a per-sample container zip, often listed at size 0
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from msdial_repository_catalog.mcp_server import msdial_catalog_reanalysis_handoff
from msdial_repository_catalog.normalize import project_to_study
from msdial_repository_catalog.storage import Catalog, download_plan

WORKBENCH = "https://www.metabolomicsworkbench.org/studydownload/"
GB = 1024**3


def workbench_study() -> dict:
    """Two polarities of one study, whose two archives name neither: every unit lists both."""
    archives = [
        {"name": "ST900001_Rawdata_A.zip", "size_bytes": 10 * GB, "url": WORKBENCH + "ST900001_Rawdata_A.zip",
         "checksum": "a" * 32, "role": "shared_raw_archive"},
        {"name": "ST900001_Rawdata_B.zip", "size_bytes": 5 * GB, "url": WORKBENCH + "ST900001_Rawdata_B.zip",
         "checksum": "b" * 32, "role": "shared_raw_archive"},
    ]
    return {
        "repository": "metabolomics_workbench",
        "accession": "ST900001",
        "title": "Shared archives",
        "analysis_units": [
            {
                "source_subrecord_id": analysis,
                "separation": "LC-MS",
                "ion_mode": mode,
                "acquisition_mode": "DDA",
                "sample_metadata": [{"sample_id": f"S{index}", "raw_file": f"S{index}_{mode[:3]}.raw"}
                                    for index in (1, 2)],
                "files": [dict(item) for item in archives],
            }
            for analysis, mode in (("AN900001", "Positive"), ("AN900002", "Negative"))
        ],
    }


def other_workbench_study() -> dict:
    return {
        "repository": "metabolomics_workbench",
        "accession": "ST900002",
        "analysis_units": [{
            "source_subrecord_id": "AN900003",
            "separation": "LC-MS",
            "ion_mode": "Positive",
            "acquisition_mode": "DDA",
            "sample_metadata": [{"sample_id": "T1", "raw_file": "T1.raw"}],
            "files": [{"name": "ST900002.zip", "size_bytes": 2 * GB, "url": WORKBENCH + "ST900002.zip",
                       "checksum": "c" * 32, "role": "raw_archive"}],
        }],
    }


def mbpost_study() -> dict:
    url = "https://repository.massbank.jp/api/download/MPST900003.1"
    return {
        "repository": "mb_post",
        "accession": "MPST900003",
        "analysis_units": [{
            "source_subrecord_id": "preset-1",
            "separation": "LC-MS",
            "ion_mode": "Positive",
            "acquisition_mode": "DDA",
            "sample_metadata": [{"sample_id": f"M{index}", "raw_file": f"M{index}.d.zip"} for index in (1, 2, 3)],
            "files": [
                {"name": f"M{index}.d.zip", "size_bytes": size, "url": url, "checksum": f"{index}" * 32,
                 "role": "raw", "sample_id": f"M{index}"}
                for index, size in ((1, 100), (2, 200), (3, 300))
            ],
        }],
    }


def metabolights_study() -> dict:
    root = "https://ftp.ebi.ac.uk/pub/databases/metabolights/studies/public/MTBLS900004/FILES/"
    return {
        "repository": "metabolights",
        "accession": "MTBLS900004",
        "analysis_units": [{
            "source_subrecord_id": "a_MTBLS900004_pos.txt",
            "separation": "LC-MS",
            "ion_mode": "Positive",
            "acquisition_mode": "DDA",
            "sample_metadata": [
                {"sample_id": "L1", "raw_file": "FILES/L1.raw.zip"},
                {"sample_id": "L2", "raw_file": "FILES/L2 run.raw"},
            ],
            "files": [
                # The public index gave no size for the container zip, and the adapter stored 0.
                {"name": "FILES/L1.raw.zip", "size_bytes": 0, "url": root + "L1.raw.zip", "role": "raw"},
                {"name": "FILES/L2 run.raw", "size_bytes": 700, "url": root + "L2%20run.raw", "role": "raw"},
            ],
        }],
    }


def fileless_study() -> dict:
    return {
        "repository": "metabolomics_workbench",
        "accession": "ST900005",
        "analysis_units": [{
            "source_subrecord_id": "AN900005",
            "separation": "LC-MS",
            "ion_mode": "Negative",
            "acquisition_mode": "DDA",
            "sample_metadata": [{"sample_id": "N1", "raw_file": "N1.raw"}],
            "files": [],
        }],
    }


class DownloadObjectFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "catalog.sqlite"
        self.units: dict[str, list[str]] = {}
        with Catalog(self.database) as catalog:
            for project in (workbench_study(), other_workbench_study(), mbpost_study(),
                            metabolights_study(), fileless_study()):
                study = project_to_study(project)
                catalog.ingest_study(study)
                self.units[study.accession] = [unit.unit_id for unit in study.analysis_units]

    def tearDown(self) -> None:
        self.directory.cleanup()

    def objects(self, unit_id: str) -> dict[str, dict]:
        with Catalog(self.database) as catalog:
            unit = catalog.get_unit(unit_id)
            objects = catalog.download_objects([item["download_url"] for item in unit["files"]])
        return {item["name"]: item for item in objects}


class AnObjectNamesEveryUnitThatNeedsIt(DownloadObjectFixture):
    def test_a_shared_workbench_archive_lists_every_consumer_unit(self) -> None:
        positive, negative = self.units["ST900001"]

        objects = self.objects(positive)

        self.assertEqual({"ST900001_Rawdata_A.zip", "ST900001_Rawdata_B.zip"}, set(objects))
        for name in objects:
            self.assertEqual(
                sorted([positive, negative]), objects[name]["consumer_unit_ids"],
                "the negative-mode unit claims the archive too, so a store must not release it "
                "when the positive unit is done",
            )
            self.assertEqual(2, objects[name]["shared_unit_count"])
            self.assertEqual("archive", objects[name]["kind"])
        archive = objects["ST900001_Rawdata_A.zip"]
        self.assertEqual(10 * GB, archive["bytes"], "counted once, not once per consumer")
        self.assertTrue(archive["size_known"])
        self.assertEqual(("a" * 32, "md5", "object"),
                         (archive["checksum"], archive["checksum_algorithm"], archive["checksum_scope"]))
        self.assertEqual(("metabolomics_workbench", "ST900001"), (archive["repository"], archive["accession"]))

    def test_units_of_another_study_are_not_consumers(self) -> None:
        (unit,) = self.units["ST900002"]

        objects = self.objects(unit)

        self.assertEqual([unit], objects["ST900002.zip"]["consumer_unit_ids"])

    def test_an_mbpost_object_is_named_accession_tar(self) -> None:
        (unit,) = self.units["MPST900003"]

        objects = self.objects(unit)

        self.assertEqual(["MPST900003.tar"], list(objects),
                         "the project URL is one tar, stored as <accession>.tar as Interactive names it")
        tar = objects["MPST900003.tar"]
        self.assertEqual("bundle", tar["kind"])
        self.assertEqual(3, tar["path_count"])
        self.assertEqual(600, tar["bytes"], "the tar carries every listed file once")
        self.assertEqual(("", "", "members"),
                         (tar["checksum"], tar["checksum_algorithm"], tar["checksum_scope"]),
                         "the MD5s are the members'; none of them vouches for the tar")

    def test_a_size_zero_row_gives_size_known_false(self) -> None:
        (unit,) = self.units["MTBLS900004"]

        objects = self.objects(unit)

        container = objects["L1.raw.zip"]
        self.assertFalse(container["size_known"])
        self.assertIsNone(container["bytes"], "0 bytes is never reported as a known size")
        self.assertEqual(0, container["known_bytes"])
        self.assertEqual(1, container["unknown_size_paths"])
        self.assertEqual("file", container["kind"])
        listed = objects["L2 run.raw"]
        self.assertEqual(700, listed["bytes"], "a percent-encoded URL keeps the file's own name")
        self.assertTrue(listed["size_known"])
        self.assertEqual(("", "", "none"),
                         (listed["checksum"], listed["checksum_algorithm"], listed["checksum_scope"]))

    def test_the_scope_says_its_bundle_bytes_are_a_lower_bound(self) -> None:
        (unit,) = self.units["MTBLS900004"]
        with Catalog(self.database) as catalog:
            urls = [item["download_url"] for item in catalog.get_unit(unit)["files"]]
            scope = catalog.download_scope(urls)
            unindexed = catalog.download_scope([*urls, "https://example.org/not-indexed.raw"])

        self.assertEqual(700, scope["bundle_bytes"], "what the listings state, as before")
        self.assertFalse(scope["bundle_size_known"])
        self.assertEqual((2, 1), (scope["object_count"], scope["unknown_size_object_count"]))
        self.assertEqual(["https://example.org/not-indexed.raw"], unindexed["unindexed_urls"])
        self.assertFalse(unindexed["bundle_size_known"])

    def test_no_url_at_all_is_no_known_size(self) -> None:
        with Catalog(self.database) as catalog:
            nothing = catalog.download_scope([])

        self.assertEqual((0, 0), (nothing["bundle_bytes"], nothing["object_count"]))
        self.assertFalse(nothing["bundle_size_known"], "nothing listed is not a known 0 bytes")


class TheSizeRuleIsPerPath(unittest.TestCase):
    """Rows written directly: this is a property of the aggregation, not of any adapter."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.catalog = Catalog(Path(self.directory.name) / "catalog.sqlite")
        self.catalog.initialize()
        connection = self.catalog.connection
        connection.execute(
            "INSERT INTO study (study_id, repository, accession, source_hash) VALUES ('s', 'example', 'EX', 'x')"
        )
        connection.executemany(
            "INSERT INTO analysis_unit (unit_id, study_id, source_subrecord_id, signature) VALUES (?, 's', ?, 'x')",
            [("u1", "u1"), ("u2", "u2"), ("u3", "u3")],
        )
        connection.executemany(
            "INSERT INTO raw_file (file_id, unit_id, path, size_bytes, checksum, download_url) VALUES (?, ?, ?, ?, ?, ?)",
            [
                # One unit's row lost the size and another's kept it: the path has a listed size.
                ("f1", "u1", "a.raw", 0, "", "https://example.org/a.raw"),
                ("f2", "u2", "a.raw", 50, "", "https://example.org/a.raw"),
                # A bundle with one member of unlisted size.
                ("f3", "u1", "b1.raw", 10, "", "https://example.org/bundle"),
                ("f4", "u1", "b2.raw", 0, "", "https://example.org/bundle"),
                # Two rows of one file that disagree on its checksum.
                ("f5", "u1", "c.raw", 5, "d" * 32, "https://example.org/c.raw"),
                ("f6", "u2", "c.raw", 5, "e" * 32, "https://example.org/c.raw"),
                # A Bruker marker file as MetaboBank lists it: size 0 and the MD5 of zero bytes.
                ("f7", "u1", "x.d/lock.file", 0, "D41D8CD98F00B204E9800998ECF8427E", "https://example.org/x.d/lock.file"),
                # A Workbench archive name with an unencoded '#', as ST001957 lists four of them.
                ("f8", "u1", "EX_Method#1_Raw.7z", 9, "", "https://example.org/studydownload/EX_Method#1_Raw.7z"),
                # A unit with one file of listed size and URL, and one whose row carries no URL.
                ("f9", "u3", "e.raw", 7, "", "https://example.org/e.raw"),
                ("f10", "u3", "f.raw", 11, "", ""),
            ],
        )
        connection.commit()

    def tearDown(self) -> None:
        self.catalog.close()
        self.directory.cleanup()

    def test_a_size_listed_by_any_row_of_the_path_is_known(self) -> None:
        (item,) = self.catalog.download_objects(["https://example.org/a.raw"])

        self.assertTrue(item["size_known"])
        self.assertEqual(50, item["bytes"])

    def test_a_bundle_with_an_unlisted_member_is_known_only_in_part(self) -> None:
        (item,) = self.catalog.download_objects(["https://example.org/bundle"])

        self.assertFalse(item["size_known"])
        self.assertIsNone(item["bytes"])
        self.assertEqual(10, item["known_bytes"], "the stated part, as a lower bound")

    def test_a_zero_with_the_empty_checksum_is_still_no_listed_size(self) -> None:
        (item,) = self.catalog.download_objects(["https://example.org/x.d/lock.file"])

        self.assertFalse(item["size_known"], "the MD5 of zero bytes is evidence, not a listed size")
        self.assertIsNone(item["bytes"])
        self.assertEqual((0, 1, 1), (item["known_bytes"], item["unknown_size_paths"], item["empty_digest_paths"]),
                         "reported apart, for a disk guard whose policy accepts it")
        self.assertEqual(("md5", "object"), (item["checksum_algorithm"], item["checksum_scope"]))

    def test_the_totals_count_objects_empty_only_by_digest_apart(self) -> None:
        plan = download_plan(self.catalog.connection, ["u1"])
        (unit,) = plan["units"]
        (bundle,) = self.catalog.download_objects(["https://example.org/bundle"])

        self.assertEqual((2, 1), (plan["unknown_size_objects"], plan["empty_digest_objects"]),
                         "the bundle's unlisted member carries no checksum; lock.file carries the empty MD5")
        self.assertEqual((2, 1), (unit["unknown_size_objects"], unit["empty_digest_objects"]))
        self.assertFalse(unit["size_known"])
        self.assertEqual(0, bundle["empty_digest_paths"])

    def test_a_file_without_a_url_leaves_its_unit_of_unknown_size(self) -> None:
        plan = download_plan(self.catalog.connection, ["u3"])
        (unit,) = plan["units"]
        scope = self.catalog.download_scope(["https://example.org/e.raw", ""])

        self.assertEqual((1, 1, 7), (unit["object_count"], unit["files_without_url"], unit["known_bytes"]))
        self.assertFalse(unit["size_known"], "a file with no URL has a size nobody can fetch or count")
        self.assertIsNone(unit["bytes"])
        self.assertEqual(1, plan["units_of_unknown_size"])
        self.assertEqual(1, plan["groups"][0]["files_without_url"])
        self.assertEqual((7, 1), (scope["bundle_bytes"], scope["files_without_url"]))
        self.assertFalse(scope["bundle_size_known"])

    def test_a_hash_in_a_file_name_is_not_a_fragment(self) -> None:
        (item,) = self.catalog.download_objects(["https://example.org/studydownload/EX_Method#1_Raw.7z"])

        self.assertEqual("EX_Method#1_Raw.7z", item["name"], "the archive suffix the store routes by survives")

    def test_rows_that_disagree_on_a_checksum_vouch_for_nothing(self) -> None:
        (item,) = self.catalog.download_objects(["https://example.org/c.raw"])

        self.assertEqual(("", "conflicting"), (item["checksum"], item["checksum_scope"]))
        self.assertEqual(["d" * 32, "e" * 32], item["declared_checksums"])


class TheHandoffCarriesTheObjects(DownloadObjectFixture):
    def test_the_handoff_file_lists_objects_and_the_response_points_to_it(self) -> None:
        positive, negative = self.units["ST900001"]

        response = msdial_catalog_reanalysis_handoff(positive, database=str(self.database))
        written = json.loads(Path(response["handoff_path"]).read_text(encoding="utf-8"))

        objects = written["download_scope"]["objects"]
        self.assertEqual(2, len(objects))
        self.assertTrue(all(item["consumer_unit_ids"] == sorted([positive, negative]) for item in objects))
        self.assertEqual(15 * GB, written["download_scope"]["bundle_bytes"])
        self.assertTrue(written["download_scope"]["bundle_size_known"])
        self.assertEqual([], response["download_scope"]["objects"])
        self.assertTrue(response["download_scope"]["objects_omitted"])
        self.assertEqual(2, response["download_scope"]["object_count"])

    def test_the_handoff_of_a_unit_that_lists_no_file_does_not_know_its_size(self) -> None:
        (unit,) = self.units["ST900005"]

        scope = msdial_catalog_reanalysis_handoff(unit, database=str(self.database))["download_scope"]

        self.assertEqual((0, 0, 0), (scope["bundle_bytes"], scope["object_count"], scope["file_count"]))
        self.assertFalse(scope["bundle_size_known"], "no listed file is not a known 0-byte bundle")


class APlannerSeesDistinctObjectsAndSharingGroups(DownloadObjectFixture):
    def plan(self, connection: sqlite3.Connection | None = None) -> dict:
        units = [
            *self.units["ST900001"], *self.units["ST900002"], *self.units["MPST900003"],
            *self.units["MTBLS900004"], *self.units["ST900005"], "not-a-unit",
        ]
        if connection is not None:
            return download_plan(connection, units)
        with Catalog(self.database) as catalog:
            return catalog.download_plan(units)

    def test_distinct_bytes_count_each_object_once_and_never_price_an_unknown_at_zero(self) -> None:
        plan = self.plan()

        self.assertEqual(6, plan["distinct_objects"], "two shared archives, one archive, one tar, two files")
        self.assertEqual(10 * GB + 5 * GB + 2 * GB + 600 + 700, plan["distinct_bytes"])
        self.assertEqual(plan["distinct_bytes"], plan["distinct_bytes_lower_bound"],
                         "the one unknown object states no part of its size")
        self.assertEqual(1, plan["unknown_size_objects"])
        self.assertEqual(2 * 15 * GB + 2 * GB + 600 + 700, plan["per_unit_known_bytes"],
                         "fetched per unit, the shared archives would move twice")
        self.assertEqual(2, plan["shared_objects"])

    def test_per_unit_bytes_and_unknown_counts(self) -> None:
        plan = self.plan()
        units = {item["unit_id"]: item for item in plan["units"]}
        positive, negative = self.units["ST900001"]
        (lights,) = self.units["MTBLS900004"]

        self.assertEqual(15 * GB, units[positive]["bytes"])
        self.assertEqual(2, units[positive]["shared_object_count"])
        self.assertIsNone(units[lights]["bytes"], "one of its objects has no listed size")
        self.assertEqual(700, units[lights]["known_bytes"])
        self.assertEqual(1, units[lights]["unknown_size_objects"])
        self.assertEqual(self.units["ST900005"], plan["units_without_objects"])
        self.assertEqual(["not-a-unit"], plan["unknown_unit_ids"])

    def test_a_unit_that_lists_no_file_is_not_priced_at_zero(self) -> None:
        plan = self.plan()
        (fileless,) = self.units["ST900005"]
        entry = next(item for item in plan["units"] if item["unit_id"] == fileless)

        self.assertFalse(entry["size_known"], "nothing says what it needs, which is not 0 bytes")
        self.assertIsNone(entry["bytes"])
        self.assertEqual((0, 0), (entry["object_count"], entry["known_bytes"]))
        self.assertEqual(2, plan["units_of_unknown_size"], "the MetaboLights unit and the fileless one")

    def test_units_that_share_an_object_form_one_group(self) -> None:
        plan = self.plan()
        positive, negative = self.units["ST900001"]

        largest = plan["groups"][0]
        self.assertEqual(sorted([positive, negative]), largest["unit_ids"])
        self.assertEqual((2, 2), (largest["object_count"], largest["shared_object_count"]))
        self.assertEqual(15 * GB, largest["distinct_bytes"])
        self.assertEqual(30 * GB, largest["per_unit_known_bytes"])
        self.assertEqual(["ST900001"], largest["accessions"])
        self.assertEqual(4, plan["group_count"], "the pair, then three units that share nothing")
        units = {item["unit_id"]: item for item in plan["units"]}
        self.assertEqual(units[positive]["group_id"], units[negative]["group_id"])
        self.assertEqual("", units[self.units["ST900005"][0]]["group_id"])

    def test_a_planner_reads_through_a_read_only_connection(self) -> None:
        connection = sqlite3.connect(f"{self.database.resolve().as_uri()}?mode=ro", uri=True)
        try:
            plan = self.plan(connection)
        finally:
            connection.close()

        self.assertEqual(self.plan()["distinct_bytes"], plan["distinct_bytes"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

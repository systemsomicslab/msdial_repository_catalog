"""No catalog update while a campaign holds the catalog, and a dead holder is reported, not overruled.

A catalog update rewrites the rows a campaign depends on: a unit whose signature changes is deleted
with its ratified Class proposal and its run records, and a unit that survives has its samples
rewritten under a proposal approved by digest. The campaign lock is the record the update code reads
instead of a rule someone remembers. These tests hold every update path to it -- the MCP tool, the
update job, the crawler and the ingest itself -- and hold the lock to two rules about its owner:
liveness is read without os.kill, which on Windows terminates the process it was meant to probe, and
a lock whose owner died still holds until the campaign releases it.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from msdial_repository_catalog import campaign_lock
from msdial_repository_catalog.campaign_lock import (
    CampaignLockedError,
    acquire_campaign_lock,
    campaign_lock_path,
    campaign_lock_status,
    release_campaign_lock,
)
from msdial_repository_catalog.crawler import CatalogCrawler
from msdial_repository_catalog.mcp_server import msdial_catalog_update_start, msdial_catalog_update_status
from msdial_repository_catalog.normalize import project_to_study
from msdial_repository_catalog.storage import Catalog
from msdial_repository_catalog.update_jobs import UpdateJobManager

from test_update_jobs import project

APPROVAL = "approval-2026-09-30-declared"
NO_OS_KILL = mock.patch("os.kill", side_effect=AssertionError("os.kill is not a liveness probe on Windows"))


class RecordingAdapter:
    calls: list[str] = []

    def __init__(self, repository: str) -> None:
        self.name = repository

    def list_accessions(self) -> list[str]:
        self.calls.append("list_accessions")
        return ["MPST-UPDATE-TEST", "MPST-NEW-1"]

    def inspect_metadata(self, accession: str) -> dict:
        self.calls.append(accession)
        return project(self.name, accession)


def exited_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def write_lock(database: Path, **record: object) -> Path:
    path = campaign_lock_path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"approval_id": APPROVAL, "host": socket.gethostname(), **record}), encoding="utf-8")
    return path


class LockFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "catalog-data" / "catalog.sqlite"
        with Catalog(self.database) as catalog:
            catalog.initialize()
        RecordingAdapter.calls = []

    def tearDown(self) -> None:
        self.directory.cleanup()

    def studies(self) -> int:
        with Catalog(self.database) as catalog:
            return catalog.connection.execute("SELECT COUNT(*) FROM study").fetchone()[0]


class AnUpdateRefusesWhileTheLockIsHeld(LockFixture):
    def setUp(self) -> None:
        super().setUp()
        acquire_campaign_lock(self.database, APPROVAL, campaign_id="declared-pool")

    def test_the_lock_lives_beside_the_database(self) -> None:
        path = self.database.parent / "campaign.lock"
        record = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual((APPROVAL, os.getpid()), (record["approval_id"], record["pid"]))

    def test_the_mcp_tool_refuses_and_starts_nothing(self) -> None:
        result = msdial_catalog_update_start(["mb_post"], confirmed=True, database=str(self.database))
        status = msdial_catalog_update_status(database=str(self.database))

        self.assertFalse(result["started"])
        self.assertEqual("campaign_lock", result["refused"])
        self.assertEqual(APPROVAL, result["campaign_lock"]["approval_id"])
        self.assertEqual("idle", status["state"])

    def test_the_preview_says_so_too(self) -> None:
        result = msdial_catalog_update_start(["mb_post"], database=str(self.database))

        self.assertEqual("campaign_lock", result["refused"])
        self.assertNotIn("confirmation_required", result, "there is nothing to confirm")

    def test_the_update_job_refuses_before_contacting_a_repository(self) -> None:
        manager = UpdateJobManager(self.database, adapter_factory=RecordingAdapter)

        with self.assertRaises(CampaignLockedError) as raised:
            manager.start(["mb_post"], mode="discover")

        self.assertEqual([], RecordingAdapter.calls)
        self.assertEqual("idle", manager.status()["state"])
        self.assertTrue(raised.exception.report["locked"])
        self.assertIsInstance(raised.exception, RuntimeError, "the GUI answers it with 409 Conflict")

    def test_the_crawler_and_the_ingest_refuse_as_well(self) -> None:
        with Catalog(self.database) as catalog:
            with self.assertRaises(CampaignLockedError):
                CatalogCrawler(catalog).sync(RecordingAdapter("mb_post"))
            with self.assertRaises(CampaignLockedError):
                catalog.ingest_study(project_to_study(project()))

        self.assertEqual([], RecordingAdapter.calls)
        self.assertEqual(0, self.studies())

    def test_reading_and_recording_still_work_under_the_lock(self) -> None:
        with Catalog(self.database) as catalog:
            stats = catalog.stats()

        self.assertEqual(0, stats["studies"])

    def test_once_released_the_update_runs(self) -> None:
        released = release_campaign_lock(self.database, APPROVAL)
        manager = UpdateJobManager(self.database, adapter_factory=RecordingAdapter)
        manager.start(["mb_post"], mode="discover")
        result = manager.wait(timeout=10)

        self.assertTrue(released["released"])
        self.assertFalse(released["was_stale"])
        self.assertEqual("completed", result["state"])
        self.assertEqual(2, self.studies())


class ALockTakenMidUpdateStopsIt(LockFixture):
    def test_the_update_stops_at_the_next_upsert_and_says_why(self) -> None:
        database = self.database

        class LockingAdapter(RecordingAdapter):
            def inspect_metadata(self, accession: str) -> dict:
                if not campaign_lock_path(database).exists():
                    acquire_campaign_lock(database, APPROVAL)  # a campaign starts meanwhile
                return super().inspect_metadata(accession)

        manager = UpdateJobManager(self.database, adapter_factory=LockingAdapter)
        manager.start(["mb_post"], mode="discover")
        result = manager.wait(timeout=10)

        self.assertEqual("cancelled", result["state"])
        self.assertIn("campaign lock", result["message"])
        self.assertEqual(APPROVAL, result["campaign_lock"]["approval_id"])
        self.assertEqual(["list_accessions", "MPST-UPDATE-TEST"], RecordingAdapter.calls,
                         "the second record was never read")
        self.assertEqual(0, self.studies(), "the refused upsert wrote nothing")
        with Catalog(self.database) as catalog:
            crawl = catalog.connection.execute("SELECT status FROM crawl_run").fetchone()[0]
        self.assertEqual("cancelled", crawl)


class AWriteInFlightFinishesBeforeTheLockIsTaken(LockFixture):
    """Taking the lock and an upsert already writing are serialised through SQLite's one write lock."""

    def title(self) -> str:
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute("SELECT title FROM study WHERE accession = 'MPST-RACE'").fetchone()[0]
        finally:
            connection.close()

    def test_acquire_returns_only_after_the_upsert_in_flight_has_committed(self) -> None:
        with Catalog(self.database) as catalog:
            catalog.ingest_study(project_to_study(project("mb_post", "MPST-RACE")))
        changed = project("mb_post", "MPST-RACE")
        changed["title"] = "rewritten by the update that was already writing"
        writing, proceed = threading.Event(), threading.Event()
        original = Catalog._ingest_unit
        seen: dict[str, object] = {}

        def ingest_unit(catalog: Catalog, study_id: str, unit: object) -> None:
            writing.set()
            proceed.wait(10)
            original(catalog, study_id, unit)

        def upsert() -> None:
            with Catalog(self.database) as catalog:
                catalog.ingest_study(project_to_study(changed))

        def take() -> None:
            seen["report"] = acquire_campaign_lock(self.database, APPROVAL)
            seen["title"] = self.title()  # what the campaign reads once it holds the catalog

        with mock.patch.object(Catalog, "_ingest_unit", ingest_unit):
            updater = threading.Thread(target=upsert)
            updater.start()
            self.assertTrue(writing.wait(10))
            campaign = threading.Thread(target=take)
            campaign.start()
            campaign.join(0.5)
            waited = campaign.is_alive()
            proceed.set()
            updater.join(10)
            campaign.join(10)

        self.assertTrue(waited, "acquire waits for the write transaction in flight")
        self.assertTrue(seen["report"]["acquired"])
        self.assertEqual(changed["title"], seen["title"], "nothing commits after acquire returns")
        with Catalog(self.database) as catalog, self.assertRaises(CampaignLockedError):
            catalog.ingest_study(project_to_study(project("mb_post", "MPST-RACE")))
        self.assertEqual(changed["title"], self.title())

    def test_an_upsert_past_the_first_check_refuses_under_the_write_lock(self) -> None:
        original = Catalog.initialize

        def initialize(catalog: Catalog) -> None:
            original(catalog)
            if not campaign_lock_path(self.database).exists():
                acquire_campaign_lock(self.database, APPROVAL)  # a campaign starts between the two reads

        with mock.patch.object(Catalog, "initialize", initialize), Catalog(self.database) as catalog:
            with self.assertRaises(CampaignLockedError):
                catalog.ingest_study(project_to_study(project()))

        self.assertEqual(0, self.studies(), "the refused upsert rolled back")

    def test_a_write_that_outlasts_the_wait_leaves_no_lock(self) -> None:
        holder = sqlite3.connect(self.database)
        holder.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(CampaignLockedError) as raised:
                acquire_campaign_lock(self.database, APPROVAL, write_wait_seconds=0.2)
        finally:
            holder.rollback()
            holder.close()

        self.assertIn("write", str(raised.exception))
        self.assertFalse(campaign_lock_status(self.database)["locked"],
                         "a lock that an upsert could still commit under is not taken")
        self.assertTrue(acquire_campaign_lock(self.database, APPROVAL)["acquired"])

    def test_a_crawl_marked_running_is_reported_not_refused(self) -> None:
        with Catalog(self.database) as catalog:
            catalog.start_crawl("mb_post", "test", 2)  # a live job, or one that died and left its row

        report = acquire_campaign_lock(self.database, APPROVAL)

        self.assertTrue(report["acquired"])
        self.assertEqual(1, report["crawls_marked_running"])

    def test_a_catalog_not_created_yet_is_not_created_by_the_lock(self) -> None:
        database = Path(self.directory.name) / "elsewhere" / "catalog.sqlite"

        report = acquire_campaign_lock(database, APPROVAL)

        self.assertTrue(report["acquired"])
        self.assertFalse(database.exists())


class AStaleLockIsReportedNotBroken(LockFixture):
    def test_a_dead_owner_is_reported_and_the_update_still_refuses(self) -> None:
        write_lock(self.database, pid=exited_pid())

        with NO_OS_KILL:
            status = campaign_lock_status(self.database)
            refused = msdial_catalog_update_start(["mb_post"], confirmed=True, database=str(self.database))

        self.assertEqual(("dead", True), (status["owner"], status["stale"]))
        self.assertIn("stale", status["message"])
        self.assertEqual("campaign_lock", refused["refused"])
        self.assertTrue(refused["campaign_lock"]["stale"])
        self.assertTrue(campaign_lock_path(self.database).exists(), "nothing removed it on its own")

    def test_the_campaign_releases_its_stale_lock_by_approval_id(self) -> None:
        write_lock(self.database, pid=exited_pid())

        with self.assertRaises(CampaignLockedError):
            release_campaign_lock(self.database, "another-approval")
        with self.assertRaises(CampaignLockedError):
            acquire_campaign_lock(self.database, APPROVAL)
        released = release_campaign_lock(self.database, APPROVAL)

        self.assertTrue(released["released"])
        self.assertTrue(released["was_stale"])
        self.assertFalse(campaign_lock_status(self.database)["locked"])

    def test_a_reused_process_id_is_a_dead_owner(self) -> None:
        created = campaign_lock._process_probe(os.getpid())[1]
        if created is None:
            self.skipTest("this platform does not report a process creation time")
        write_lock(self.database, pid=os.getpid(), process_created_at=created - 1000)

        with NO_OS_KILL:
            status = campaign_lock_status(self.database)

        self.assertEqual("dead", status["owner"])
        self.assertFalse(status["held_by_this_process"])

    def test_an_owner_on_another_host_cannot_be_judged(self) -> None:
        write_lock(self.database, pid=os.getpid(), host="another-machine")

        status = campaign_lock_status(self.database)
        with self.assertRaises(CampaignLockedError):
            release_campaign_lock(self.database, APPROVAL)
        released = release_campaign_lock(self.database, APPROVAL, force=True)

        self.assertEqual("unknown", status["owner"])
        self.assertTrue(released["released"])

    def test_an_unreadable_lock_still_refuses(self) -> None:
        campaign_lock_path(self.database).write_text("not json", encoding="utf-8")

        status = campaign_lock_status(self.database)
        with self.assertRaises(CampaignLockedError):
            UpdateJobManager(self.database, adapter_factory=RecordingAdapter).start(["mb_post"])
        with self.assertRaises(CampaignLockedError):
            release_campaign_lock(self.database, APPROVAL)

        self.assertEqual((True, False), (status["locked"], status["readable"]))
        self.assertTrue(release_campaign_lock(self.database, APPROVAL, force=True)["released"])


class LockAndUnlock(LockFixture):
    def test_the_holder_may_take_it_again_and_nobody_else_may(self) -> None:
        first = acquire_campaign_lock(self.database, APPROVAL)
        again = acquire_campaign_lock(self.database, APPROVAL)

        with self.assertRaises(CampaignLockedError):
            acquire_campaign_lock(self.database, "another-approval")
        with self.assertRaises(CampaignLockedError):
            release_campaign_lock(self.database, "another-approval")
        with self.assertRaises(ValueError):
            acquire_campaign_lock(self.database, "  ")

        self.assertEqual((True, False), (first["acquired"], first["reentered"]))
        self.assertTrue(again["reentered"])
        self.assertTrue(first["held_by_this_process"])
        self.assertTrue(release_campaign_lock(self.database, APPROVAL)["released"])
        self.assertFalse(release_campaign_lock(self.database, APPROVAL)["released"], "nothing left to release")

    def test_a_live_owner_in_another_process_is_never_overruled(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            write_lock(self.database, pid=child.pid, process_created_at=campaign_lock._process_probe(child.pid)[1])
            with NO_OS_KILL:
                alive = campaign_lock_status(self.database)
            with self.assertRaises(CampaignLockedError):
                release_campaign_lock(self.database, APPROVAL, force=True)
        finally:
            child.kill()
            child.wait()
        with NO_OS_KILL:
            dead = campaign_lock_status(self.database)

        self.assertEqual("alive", alive["owner"])
        self.assertFalse(alive["held_by_this_process"])
        self.assertEqual("dead", dead["owner"])

    def test_concurrent_acquirers_get_one_lock(self) -> None:
        outcomes: list[str] = []

        def take(approval: str) -> None:
            try:
                acquire_campaign_lock(self.database, approval)
                outcomes.append("acquired")
            except CampaignLockedError:
                outcomes.append("refused")

        threads = [threading.Thread(target=take, args=(f"approval-{index}",)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(1, outcomes.count("acquired"))


@unittest.skipUnless(os.name == "nt", "OpenProcess is the Windows probe")
class WithoutPsutilWindowsIsReadThroughOpenProcess(unittest.TestCase):
    def test_open_process_tells_a_live_process_from_an_exited_one(self) -> None:
        with mock.patch.object(campaign_lock, "_psutil_probe", return_value=(None, None)), NO_OS_KILL:
            alive, created = campaign_lock._process_probe(os.getpid())
            dead, _ = campaign_lock._process_probe(exited_pid())

        self.assertTrue(alive)
        self.assertIsNotNone(created)
        self.assertFalse(dead)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

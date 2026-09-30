"""The campaign lock: no catalog update while a reanalysis campaign is reading this catalog.

WHY THIS EXISTS. A catalog update re-ingests studies, and re-ingesting is not neutral for the records a
campaign keeps here (see the upsert cascade in storage.Catalog.ingest_study). A unit whose technical
signature changes gets a new unit_id, and the old one is deleted with its ratified Class proposal and
its analysis_run rows; a unit that keeps its id has its sample and file rows rewritten, so the Class
proposal the campaign approved by digest no longer matches what the catalog would build. A campaign of
several hundred units runs for weeks, so "do not run an update meanwhile" has to be a record that the
update code reads, not a rule a person remembers.

WHAT IT IS. One file, <catalog data dir>/campaign.lock, beside the database, naming the campaign approval
that holds it and the process that took it. While it exists every catalog update refuses: the MCP tool,
the update job, the crawler and the ingest itself. It is created with O_CREAT|O_EXCL, so two campaigns
cannot both believe they hold it.

WHY ACQUIRE WAITS FOR THE WRITE LOCK. The file alone does not stop an upsert that is already writing:
it read "no lock" before the file existed and would commit after the campaign believed the catalog
frozen. So Catalog.ingest_study reads the lock again inside a BEGIN IMMEDIATE transaction, and acquire,
once its file exists, takes that same write lock (BEGIN IMMEDIATE, then roll back) before it returns.
SQLite has one writer, so either the upsert took the write lock first and has committed by the time
acquire returns, or it takes it afterwards and finds the file. A write that holds the database longer
than acquire will wait leaves no lock behind: acquire removes its file and refuses, and the campaign
tries again. A crawl_run row marked running is reported (crawls_marked_running), not refused on: a job
that died leaves its row running for good, and a live one stops at its next upsert.

WHY A STALE LOCK IS NOT BROKEN. A lock whose owner has died is reported as stale, and the update still
refuses. The owner died mid-campaign, which is exactly when the campaign's records are least settled;
whoever resumes it decides, by releasing the lock with its approval id. Nothing here removes a lock on
its own judgement.

WHY LIVENESS IS NOT os.kill. On Windows os.kill(pid, 0) is not a probe: signal 0 is CTRL_C_EVENT, and any
other value terminates the process. The owner is read through psutil or OpenProcess, with its creation
time, because Windows reuses process ids. These are the semantics of Interactive's process_liveness.py,
which the download store and the campaign runner use; the Catalog does not depend on Interactive, so it
carries the same probe rather than importing it.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOCK_NAME = "campaign.lock"
LOCK_SCHEMA = "msdial-catalog-campaign-lock.v1"
# Two readings of one process's creation time can differ by rounding; a reused id starts later.
CREATION_TIME_TOLERANCE_SECONDS = 1.0
# How long acquire waits for a catalog write in flight. An upsert holds the write lock for one study:
# measured at 0.5 s on a scratch catalog for 27,198 files, the size of MTBKS263, the largest study
# indexed. Ten minutes means something is stuck.
WRITE_WAIT_SECONDS = 600.0


class CampaignLockedError(RuntimeError):
    """A catalog update was refused, or a lock could not be taken or released.

    A RuntimeError, so the GUI's existing conflict path (HTTP 409) and the update job's failure path
    report it without new handling. `report` is the lock as campaign_lock_status reads it.
    """

    def __init__(self, message: str, report: dict[str, Any]) -> None:
        super().__init__(message)
        self.report = report


def campaign_lock_path(database: str | Path) -> Path:
    """The lock of the catalog stored in `database`: campaign.lock in the database's directory."""
    return Path(database).expanduser().resolve().parent / LOCK_NAME


def campaign_lock_status(database: str | Path) -> dict[str, Any]:
    """Whether a campaign holds this catalog, and whether its owner is alive. Changes nothing."""
    path = campaign_lock_path(database)
    report: dict[str, Any] = {"locked": False, "path": str(path)}
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return report
    except OSError as error:
        return {
            **report, "locked": True, "readable": False, "owner": "unknown", "stale": False,
            "message": f"A campaign lock exists at {path} and could not be read: {error}.",
        }
    report["locked"] = True
    try:
        record = json.loads(data.decode("utf-8"))
        if not isinstance(record, dict):
            raise ValueError("not a JSON object")
    except ValueError as error:
        return {
            **report, "readable": False, "owner": "unknown", "stale": False,
            "message": (
                f"A campaign lock exists at {path} and is not a lock record ({error}). Check that no "
                "campaign is running before removing it by hand."
            ),
        }
    owner = _owner_state(record)
    report.update(
        readable=True,
        approval_id=str(record.get("approval_id") or ""),
        campaign_id=str(record.get("campaign_id") or ""),
        pid=record.get("pid"),
        host=str(record.get("host") or ""),
        acquired_at=str(record.get("acquired_at") or ""),
        owner=owner,
        stale=owner == "dead",
        held_by_this_process=_is_this_process(record),
    )
    if owner == "dead":
        report["message"] = (
            f"Campaign approval {report['approval_id']} holds this catalog, but its owner (pid "
            f"{report['pid']}) is no longer running. The lock is stale and still holds: the campaign "
            "decides, by releasing it with its approval id."
        )
    else:
        report["message"] = (
            f"Campaign approval {report['approval_id']} holds this catalog (pid {report['pid']}, "
            f"owner {owner})."
        )
    return report


def refuse_while_campaign_locked(database: str | Path, action: str) -> None:
    """Raise CampaignLockedError when a campaign lock exists, whatever its owner's state."""
    report = campaign_lock_status(database)
    if report["locked"]:
        raise CampaignLockedError(
            f"Refused {action}: a campaign lock exists at {report['path']}. {report['message']} "
            "A catalog update would rewrite the rows the campaign's approved Class proposals and run "
            "records depend on.",
            report,
        )


def acquire_campaign_lock(
    database: str | Path,
    approval_id: str,
    *,
    campaign_id: str = "",
    write_wait_seconds: float = WRITE_WAIT_SECONDS,
) -> dict[str, Any]:
    """Take the lock for one campaign approval, or raise CampaignLockedError with what holds it.

    Taking it again from the process that holds it is a no-op. Any other existing lock refuses,
    stale ones included: release a stale lock first, with its approval id. A new lock is returned
    only once no catalog write is in flight (see WHY ACQUIRE WAITS FOR THE WRITE LOCK), waiting up to
    `write_wait_seconds`; past that the file is removed again and CampaignLockedError raised. The
    result's `crawls_marked_running` counts crawl_run rows still marked running.
    """
    approval = str(approval_id or "").strip()
    if not approval:
        raise ValueError("A campaign lock names the campaign approval that holds it.")
    path = campaign_lock_path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": LOCK_SCHEMA,
        "approval_id": approval,
        "campaign_id": str(campaign_id or "").strip(),
        "pid": os.getpid(),
        "process_created_at": _process_probe(os.getpid())[1],
        "host": socket.gethostname(),
        "acquired_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        report = campaign_lock_status(database)
        if report.get("held_by_this_process") and report.get("approval_id") == approval:
            return {**report, "acquired": True, "reentered": True}
        raise CampaignLockedError(
            f"The campaign lock at {path} is already held. {report.get('message', '')}", report
        ) from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        running = _wait_for_catalog_writes(database, write_wait_seconds)
    except sqlite3.Error as error:
        if campaign_lock_status(database).get("held_by_this_process"):
            path.unlink(missing_ok=True)
        raise CampaignLockedError(
            f"The campaign lock at {path} was not taken: the catalog's write lock could not be taken "
            f"within {write_wait_seconds:g} s ({error}), so a write in flight could still commit under "
            "the lock. Take the lock again once that write has finished.",
            campaign_lock_status(database),
        ) from None
    return {
        **campaign_lock_status(database), "acquired": True, "reentered": False,
        "crawls_marked_running": running,
    }


def release_campaign_lock(
    database: str | Path, approval_id: str, *, force: bool = False
) -> dict[str, Any]:
    """Remove the lock that `approval_id` holds, when its owner is this process or no longer running.

    Refuses a lock held by another approval, and one whose owner is alive in another process. An
    owner that cannot be judged (another host, an unreadable process) or a lock record that cannot be
    read refuses unless `force`; a live owner in another process refuses even then.
    """
    approval = str(approval_id or "").strip()
    path = campaign_lock_path(database)
    report = campaign_lock_status(database)
    if not report["locked"]:
        return {**report, "released": False}
    readable = report.get("readable", False)
    if readable and report.get("approval_id") != approval:
        raise CampaignLockedError(
            f"The campaign lock at {path} is held by approval {report.get('approval_id')}, not "
            f"{approval or '(none named)'}.",
            report,
        )
    owner = report.get("owner")
    if owner == "alive" and not report.get("held_by_this_process"):
        raise CampaignLockedError(
            f"The campaign lock at {path} is held by a running process (pid {report.get('pid')}).",
            report,
        )
    if (not readable or owner == "unknown") and not report.get("held_by_this_process") and not force:
        raise CampaignLockedError(
            f"The owner of the campaign lock at {path} cannot be judged. {report.get('message', '')} "
            "Release it with force=True once no campaign is running.",
            report,
        )
    path.unlink(missing_ok=True)
    return {**report, "locked": False, "released": True, "was_stale": owner == "dead"}


def _wait_for_catalog_writes(database: str | Path, timeout: float) -> int:
    """Return once no write transaction is open on `database`, with the crawl runs marked running.

    Raises sqlite3.OperationalError when a write holds the database past `timeout`. A database that
    does not exist has nothing in flight, since an upsert opens it before it writes, and is not
    created: this opens it read-write only, and never through Catalog, which would migrate it.
    """
    target = Path(database).expanduser().resolve()
    if not target.exists():
        return 0
    connection = sqlite3.connect(f"{target.as_uri()}?mode=rw", uri=True, timeout=max(0.0, timeout))
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            running = connection.execute(
                "SELECT COUNT(*) FROM crawl_run WHERE status = 'running'"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            running = 0  # an empty database, before its first initialize
        connection.rollback()
    finally:
        connection.close()
    return int(running)


def _is_this_process(record: dict[str, Any]) -> bool:
    try:
        pid = int(record.get("pid"))
    except (TypeError, ValueError):
        return False
    if pid != os.getpid() or str(record.get("host") or "") != socket.gethostname():
        return False
    return _same_creation(record.get("process_created_at"), _process_probe(pid)[1])


def _owner_state(record: dict[str, Any]) -> str:
    """alive, dead or unknown. Never signals the process."""
    if str(record.get("host") or "") != socket.gethostname():
        return "unknown"  # A process id means nothing on another machine.
    try:
        pid = int(record.get("pid"))
    except (TypeError, ValueError):
        return "unknown"
    if pid <= 0:
        return "unknown"
    alive, created = _process_probe(pid)
    if alive is None:
        return "unknown"
    if not alive:
        return "dead"
    if not _same_creation(record.get("process_created_at"), created):
        return "dead"  # The id was reused: a live process with another creation time is another process.
    return "alive"


def _same_creation(recorded: Any, created: float | None) -> bool:
    if recorded is None or created is None:
        return True  # Nothing to compare, so no evidence that the id was reused.
    try:
        return abs(float(recorded) - float(created)) <= CREATION_TIME_TOLERANCE_SECONDS
    except (TypeError, ValueError):
        return True


def _process_probe(pid: int) -> tuple[bool | None, float | None]:
    """(alive, creation time in Unix seconds). alive is None when it cannot be read."""
    alive, created = _psutil_probe(pid)
    if alive is not None:
        return alive, created
    if os.name == "nt":
        return _windows_probe(pid)
    if Path("/proc/self").exists():
        return Path(f"/proc/{pid}").exists(), None
    return None, None


def _psutil_probe(pid: int) -> tuple[bool | None, float | None]:
    try:
        import psutil
    except ImportError:
        return None, None
    try:
        process = psutil.Process(pid)
        if process.status() == psutil.STATUS_ZOMBIE:
            return False, None
        return True, process.create_time()
    except psutil.NoSuchProcess:  # includes ZombieProcess
        return False, None
    except psutil.AccessDenied:
        return True, None
    except psutil.Error:
        return None, None


def _windows_probe(pid: int) -> tuple[bool | None, float | None]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: no such process
            return False, None
        return (True if error == 5 else None), None  # ERROR_ACCESS_DENIED: it exists, not ours to read
    try:
        # OpenProcess succeeds for an exited process while any handle to it is open; the exit code
        # is what says whether it is still running.
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None, None
        if code.value != 259:  # STILL_ACTIVE
            return False, None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            return True, None
        ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return True, ticks / 10_000_000 - 11_644_473_600
    finally:
        kernel32.CloseHandle(handle)

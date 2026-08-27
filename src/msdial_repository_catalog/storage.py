from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .context import context_values, normalize_field_name, normalize_value
from .models import ClassProposal, StudyRecord, stable_id
from .schema import BASE_SCHEMA, FTS_SCHEMA, SCHEMA_VERSION


class Catalog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.fts_enabled = False

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def initialize(self) -> None:
        with self.connection:
            self.connection.executescript(BASE_SCHEMA)
            current = self.connection.execute("SELECT version FROM schema_info LIMIT 1").fetchone()
            if current is None:
                self.connection.execute("INSERT INTO schema_info(version) VALUES (?)", (SCHEMA_VERSION,))
            elif int(current["version"]) != SCHEMA_VERSION:
                raise RuntimeError(
                    f"Catalog schema {current['version']} is incompatible with {SCHEMA_VERSION}."
                )
        try:
            with self.connection:
                self.connection.executescript(FTS_SCHEMA)
            self.fts_enabled = True
        except sqlite3.OperationalError:
            self.fts_enabled = False

    def source_hash(self, repository: str, accession: str) -> str:
        self.initialize()
        row = self.connection.execute(
            "SELECT source_hash FROM study WHERE repository = ? AND accession = ?",
            (repository, accession),
        ).fetchone()
        return str(row["source_hash"]) if row else ""

    def source_state(self, repository: str, accession: str) -> dict[str, str]:
        self.initialize()
        row = self.connection.execute(
            "SELECT source_hash, parser_version FROM study WHERE repository = ? AND accession = ?",
            (repository, accession),
        ).fetchone()
        return dict(row) if row else {"source_hash": "", "parser_version": ""}

    def start_crawl(
        self, repository: str, crawler_version: str, discovered_count: int
    ) -> str:
        self.initialize()
        started = datetime.now(timezone.utc).isoformat()
        crawl_run_id = stable_id("crawl", repository, started)
        with self.connection:
            self.connection.execute(
                "INSERT INTO crawl_run(crawl_run_id, repository, started_at, crawler_version, status, discovered_count) "
                "VALUES (?, ?, ?, ?, 'running', ?)",
                (crawl_run_id, repository, started, crawler_version, discovered_count),
            )
        return crawl_run_id

    def finish_crawl(self, crawl_run_id: str, summary: Any) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE crawl_run SET completed_at = ?, status = ?, hydrated_count = ?,
                    unchanged_count = ?, failed_count = ?, details_json = ?
                WHERE crawl_run_id = ?
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    "completed_with_errors" if summary.failed else "completed",
                    summary.hydrated, summary.unchanged, summary.failed,
                    _json({"failures": summary.failures}), crawl_run_id,
                ),
            )

    def stats(self) -> dict[str, Any]:
        self.initialize()
        return {
            "database": str(self.path),
            "schema_version": SCHEMA_VERSION,
            "fts_enabled": self.fts_enabled,
            "studies": self.connection.execute("SELECT COUNT(*) FROM study").fetchone()[0],
            "analysis_units": self.connection.execute("SELECT COUNT(*) FROM analysis_unit").fetchone()[0],
            "samples": self.connection.execute("SELECT COUNT(*) FROM sample").fetchone()[0],
            "raw_files": self.connection.execute("SELECT COUNT(*) FROM raw_file").fetchone()[0],
            "class_proposals": self.connection.execute("SELECT COUNT(*) FROM class_proposal").fetchone()[0],
        }

    def overview(self) -> dict[str, Any]:
        """Return compact aggregates and filter values for the local GUI."""
        self.initialize()
        repository_rows = self.connection.execute(
            """
            SELECT s.repository, COUNT(DISTINCT s.study_id) AS studies,
                   COUNT(DISTINCT u.unit_id) AS analysis_units,
                   COUNT(DISTINCT sm.sample_pk) AS samples
            FROM study s
            LEFT JOIN analysis_unit u ON u.study_id = s.study_id
            LEFT JOIN sample sm ON sm.unit_id = u.unit_id
            GROUP BY s.repository
            ORDER BY s.repository
            """
        ).fetchall()
        review_rows = self.connection.execute(
            "SELECT review_status, COUNT(*) AS count FROM analysis_unit "
            "GROUP BY review_status ORDER BY review_status"
        ).fetchall()
        latest_crawls = self.connection.execute(
            """
            SELECT repository, started_at, completed_at, status, discovered_count,
                   hydrated_count, unchanged_count, failed_count, crawler_version
            FROM crawl_run ORDER BY started_at DESC LIMIT 12
            """
        ).fetchall()
        filters = {}
        for key, column in (
            ("repositories", "s.repository"),
            ("separations", "u.separation"),
            ("chromatographies", "u.chromatography"),
            ("ion_modes", "u.ion_mode"),
            ("acquisition_modes", "u.acquisition_mode"),
            ("target_omics", "u.target_omics"),
            ("review_statuses", "u.review_status"),
        ):
            rows = self.connection.execute(
                f"SELECT DISTINCT {column} AS value FROM analysis_unit u "
                "JOIN study s ON s.study_id = u.study_id "
                f"WHERE {column} <> '' ORDER BY {column} COLLATE NOCASE"
            ).fetchall()
            filters[key] = [str(row["value"]) for row in rows]
        return {
            **self.stats(),
            "repositories": [dict(row) for row in repository_rows],
            "review_status": [dict(row) for row in review_rows],
            "latest_crawls": [dict(row) for row in latest_crawls],
            "filters": filters,
        }

    def ingest_study(self, study: StudyRecord) -> dict[str, int | str]:
        self.initialize()
        source_hash = study.source_hash()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO study(
                    study_id, repository, accession, title, description, public_url, license,
                    source_hash, source_updated_at, retrieved_at, parser_version,
                    source_payload_json, source_urls_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(study_id) DO UPDATE SET
                    title=excluded.title, description=excluded.description,
                    public_url=excluded.public_url, license=excluded.license,
                    source_hash=excluded.source_hash, source_updated_at=excluded.source_updated_at,
                    retrieved_at=excluded.retrieved_at, parser_version=excluded.parser_version,
                    source_payload_json=excluded.source_payload_json,
                    source_urls_json=excluded.source_urls_json
                """,
                (
                    study.study_id, study.repository, study.accession, study.title,
                    study.description, study.public_url, study.license, source_hash,
                    study.source_updated_at, study.retrieved_at, study.parser_version,
                    _json(study.source_payload), _json(study.source_urls),
                ),
            )
            snapshot_id = stable_id(study.study_id, source_hash, study.parser_version)
            self.connection.execute(
                """
                INSERT OR IGNORE INTO source_snapshot(
                    snapshot_id, study_id, source_hash, retrieved_at, parser_version,
                    source_payload_json, source_urls_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id, study.study_id, source_hash, study.retrieved_at,
                    study.parser_version, _json(study.source_payload), _json(study.source_urls),
                ),
            )
            self.connection.execute("DELETE FROM publication WHERE study_id = ?", (study.study_id,))
            for publication in study.publications:
                publication_id = stable_id(
                    study.study_id, publication.get("doi"), publication.get("pubmed_id"),
                    publication.get("title"),
                )
                self.connection.execute(
                    "INSERT INTO publication VALUES (?, ?, ?, ?, ?)",
                    (
                        publication_id, study.study_id, str(publication.get("title") or ""),
                        str(publication.get("doi") or ""), str(publication.get("pubmed_id") or ""),
                    ),
                )

            incoming_units = {unit.unit_id for unit in study.analysis_units}
            existing_units = {
                str(row["unit_id"])
                for row in self.connection.execute(
                    "SELECT unit_id FROM analysis_unit WHERE study_id = ?", (study.study_id,)
                )
            }
            for stale in existing_units - incoming_units:
                self.connection.execute("DELETE FROM analysis_unit WHERE unit_id = ?", (stale,))

            sample_count = 0
            file_count = 0
            for unit in study.analysis_units:
                self._ingest_unit(study.study_id, unit)
                sample_count += len(unit.samples)
                file_count += len(unit.files)
            self._index_study(study)
        return {
            "study_id": study.study_id,
            "source_hash": source_hash,
            "analysis_units": len(study.analysis_units),
            "samples": sample_count,
            "files": file_count,
        }

    def _ingest_unit(self, study_id: str, unit: Any) -> None:
        self.connection.execute(
            """
            INSERT INTO analysis_unit(
                unit_id, study_id, source_subrecord_id, label, separation, chromatography,
                ion_mode, acquisition_mode, ion_mobility, instrument, target_omics,
                untargeted, review_status, signature, warnings_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(unit_id) DO UPDATE SET
                label=excluded.label, separation=excluded.separation,
                chromatography=excluded.chromatography, ion_mode=excluded.ion_mode,
                acquisition_mode=excluded.acquisition_mode, ion_mobility=excluded.ion_mobility,
                instrument=excluded.instrument, target_omics=excluded.target_omics,
                untargeted=excluded.untargeted, review_status=excluded.review_status,
                signature=excluded.signature, warnings_json=excluded.warnings_json
            """,
            (
                unit.unit_id, study_id, unit.source_subrecord_id, unit.label,
                unit.separation, unit.chromatography, unit.ion_mode,
                unit.acquisition_mode, unit.ion_mobility, unit.instrument,
                unit.target_omics, _db_bool(unit.untargeted), unit.review_status,
                unit.signature, _json(unit.warnings),
            ),
        )
        for table in ("analysis_unit_evidence", "analysis_unit_context", "sample", "raw_file"):
            self.connection.execute(f"DELETE FROM {table} WHERE unit_id = ?", (unit.unit_id,))
        for position, evidence in enumerate(unit.evidence):
            evidence_id = stable_id(unit.unit_id, "evidence", position, evidence.source, evidence.note)
            self.connection.execute(
                "INSERT INTO analysis_unit_evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    evidence_id, unit.unit_id, evidence.source, evidence.source_field,
                    evidence.source_value, evidence.method, evidence.confidence, evidence.note,
                ),
            )
        for index, assertion in enumerate(unit.contexts):
            evidence = assertion.evidence
            context_id = stable_id(unit.unit_id, "unit-context", index, assertion.category, assertion.value)
            self.connection.execute(
                "INSERT INTO analysis_unit_context VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    context_id, unit.unit_id, assertion.category, assertion.value,
                    assertion.normalized_value, assertion.ontology_id,
                    evidence.source if evidence else "", evidence.source_field if evidence else "",
                    evidence.source_value if evidence else "", evidence.method if evidence else "",
                    evidence.confidence if evidence else 0.6, evidence.note if evidence else "",
                ),
            )
        for position, sample in enumerate(unit.samples):
            sample_pk = stable_id(unit.unit_id, sample.sample_id, sample.raw_file, position)
            self.connection.execute(
                "INSERT INTO sample VALUES (?, ?, ?, ?, ?, ?)",
                (
                    sample_pk, unit.unit_id, sample.sample_id, sample.source_name,
                    sample.raw_file, _json(sample.attributes),
                ),
            )
            for field_name, raw_value in sample.attributes.items():
                value = normalize_value(raw_value)
                attribute_id = stable_id(sample_pk, "attribute", field_name)
                self.connection.execute(
                    "INSERT INTO sample_attribute VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        attribute_id, sample_pk, str(field_name), normalize_field_name(field_name),
                        value, value, "repository", "repository", 1.0,
                    ),
                )
            for index, assertion in enumerate(sample.contexts):
                evidence = assertion.evidence
                context_id = stable_id(sample_pk, "context", index, assertion.category, assertion.value)
                self.connection.execute(
                    "INSERT INTO sample_context VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        context_id, sample_pk, assertion.category, assertion.value,
                        assertion.normalized_value, assertion.ontology_id,
                        evidence.source if evidence else "", evidence.source_field if evidence else "",
                        evidence.source_value if evidence else "", evidence.method if evidence else "",
                        evidence.confidence if evidence else 1.0, evidence.note if evidence else "",
                    ),
                )
        for position, item in enumerate(unit.files):
            file_id = stable_id(unit.unit_id, item.path, item.role, position)
            self.connection.execute(
                "INSERT INTO raw_file VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    file_id, unit.unit_id, item.path, item.role, item.size_bytes,
                    item.checksum, item.download_url, item.sample_id,
                ),
            )

    def _index_study(self, study: StudyRecord) -> None:
        if not self.fts_enabled:
            return
        self.connection.execute("DELETE FROM study_fts WHERE study_id = ?", (study.study_id,))
        context_text = " ".join(
            " ".join(
                [
                    context_values(unit.samples),
                    *(assertion.normalized_value or assertion.value for assertion in unit.contexts),
                ]
            )
            for unit in study.analysis_units
        )
        self.connection.execute(
            "INSERT INTO study_fts(study_id, title, description, biological_context) VALUES (?, ?, ?, ?)",
            (study.study_id, study.title, study.description, context_text),
        )

    def search(
        self,
        *,
        text: str = "",
        repository: str = "",
        separation: str = "",
        chromatography: str = "",
        ion_mode: str = "",
        acquisition_mode: str = "",
        target_omics: str = "",
        biological_context: str = "",
        review_status: str = "",
        max_download_bytes: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self.initialize()
        joins = []
        where = []
        params: list[Any] = []
        if text:
            if self.fts_enabled:
                joins.append("JOIN study_fts fts ON fts.study_id = s.study_id")
                where.append("study_fts MATCH ?")
                params.append(text)
            else:
                where.append("(s.title LIKE ? OR s.description LIKE ?)")
                params.extend((f"%{text}%", f"%{text}%"))
        for column, value in (
            ("s.repository", repository), ("u.separation", separation),
            ("u.chromatography", chromatography), ("u.ion_mode", ion_mode),
            ("u.acquisition_mode", acquisition_mode), ("u.target_omics", target_omics),
            ("u.review_status", review_status),
        ):
            if value:
                where.append(f"{column} = ? COLLATE NOCASE")
                params.append(value)
        if biological_context:
            where.append(
                "(EXISTS (SELECT 1 FROM sample sm JOIN sample_context sc ON sc.sample_pk = sm.sample_pk "
                "WHERE sm.unit_id = u.unit_id AND (sc.normalized_value LIKE ? OR sc.value LIKE ?)) "
                "OR EXISTS (SELECT 1 FROM analysis_unit_context uc WHERE uc.unit_id = u.unit_id "
                "AND (uc.normalized_value LIKE ? OR uc.value LIKE ?)))"
            )
            params.extend((f"%{biological_context}%",) * 4)
        if max_download_bytes is not None:
            where.append("COALESCE((SELECT SUM(size_bytes) FROM raw_file WHERE unit_id = u.unit_id), 0) <= ?")
            params.append(max_download_bytes)
        clause = " AND ".join(where) if where else "1 = 1"
        params.append(max(1, min(int(limit), 500)))
        rows = self.connection.execute(
            f"""
            SELECT s.repository, s.accession, s.title, s.public_url, u.*,
                   (SELECT COUNT(*) FROM sample WHERE unit_id = u.unit_id) AS sample_count,
                   COALESCE((SELECT SUM(size_bytes) FROM raw_file WHERE unit_id = u.unit_id), 0) AS download_bytes
            FROM analysis_unit u JOIN study s ON s.study_id = u.study_id
            {' '.join(joins)}
            WHERE {clause}
            ORDER BY s.repository, s.accession, u.source_subrecord_id
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def get_unit(self, unit_id: str) -> dict[str, Any]:
        self.initialize()
        unit = self.connection.execute(
            "SELECT u.*, s.repository, s.accession, s.title, s.description, s.public_url "
            "FROM analysis_unit u JOIN study s ON s.study_id = u.study_id WHERE u.unit_id = ?",
            (unit_id,),
        ).fetchone()
        if unit is None:
            raise KeyError(f"Unknown analysis unit: {unit_id}")
        result = dict(unit)
        result["warnings"] = json.loads(result.pop("warnings_json"))
        samples = []
        for sample in self.connection.execute(
            "SELECT * FROM sample WHERE unit_id = ? ORDER BY sample_id, raw_file", (unit_id,)
        ):
            item = dict(sample)
            item["attributes"] = json.loads(item.pop("attributes_json"))
            item["contexts"] = [
                dict(row)
                for row in self.connection.execute(
                    "SELECT category, value, normalized_value, ontology_id, source, source_field, "
                    "source_value, method, confidence, note FROM sample_context WHERE sample_pk = ?",
                    (item["sample_pk"],),
                )
            ]
            samples.append(item)
        result["samples"] = samples
        result["contexts"] = [
            dict(row) for row in self.connection.execute(
                "SELECT category, value, normalized_value, ontology_id, source, source_field, "
                "source_value, method, confidence, note FROM analysis_unit_context WHERE unit_id = ?",
                (unit_id,),
            )
        ]
        result["files"] = [
            dict(row) for row in self.connection.execute(
                "SELECT path, role, size_bytes, checksum, download_url, sample_id "
                "FROM raw_file WHERE unit_id = ? ORDER BY path", (unit_id,)
            )
        ]
        return result

    def save_class_proposal(self, proposal: ClassProposal) -> None:
        from .class_proposal import validate_class_proposal

        validate_class_proposal(self.get_unit(proposal.unit_id), proposal)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO class_proposal VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(proposal_id) DO UPDATE SET
                    selected_fields_json=excluded.selected_fields_json,
                    rationale=excluded.rationale,
                    contrast_definition_json=excluded.contrast_definition_json,
                    model=excluded.model, prompt_hash=excluded.prompt_hash,
                    status=excluded.status, warnings_json=excluded.warnings_json
                """,
                (
                    proposal.proposal_id, proposal.unit_id, proposal.purpose,
                    _json(proposal.selected_fields), proposal.rationale,
                    _json(proposal.contrast_definition), proposal.model, proposal.prompt_hash,
                    proposal.status, _json(proposal.warnings), datetime.now(timezone.utc).isoformat(),
                ),
            )
            self.connection.execute(
                "DELETE FROM class_assignment WHERE proposal_id = ?", (proposal.proposal_id,)
            )
            self.connection.executemany(
                "INSERT INTO class_assignment VALUES (?, ?, ?, ?)",
                [
                    (proposal.proposal_id, item.sample_id, item.class_label, _json(item.values))
                    for item in proposal.assignments
                ],
            )

    def get_class_proposal(self, proposal_id: str) -> dict[str, Any]:
        self.initialize()
        row = self.connection.execute(
            "SELECT * FROM class_proposal WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown Class proposal: {proposal_id}")
        result = dict(row)
        for source, target in (
            ("selected_fields_json", "selected_fields"),
            ("contrast_definition_json", "contrast_definition"),
            ("warnings_json", "warnings"),
        ):
            result[target] = json.loads(result.pop(source))
        result["assignments"] = [
            {
                **dict(assignment),
                "values": json.loads(assignment["values_json"]),
            }
            for assignment in self.connection.execute(
                "SELECT sample_id, class_label, values_json FROM class_assignment "
                "WHERE proposal_id = ? ORDER BY sample_id", (proposal_id,)
            )
        ]
        for assignment in result["assignments"]:
            assignment.pop("values_json", None)
        return result

    def snapshot(
        self, destination: str | Path, include_local_decisions: bool = False
    ) -> dict[str, Any]:
        self.connection.commit()
        destination = Path(destination).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as temporary:
            compact = Path(temporary) / "catalog.sqlite"
            target = sqlite3.connect(compact)
            try:
                self.connection.backup(target)
            finally:
                target.close()
            if not include_local_decisions:
                target = sqlite3.connect(compact)
                try:
                    target.execute("PRAGMA foreign_keys = ON")
                    target.execute("DELETE FROM class_assignment")
                    target.execute("DELETE FROM class_proposal")
                    target.execute("DELETE FROM manual_override")
                    target.commit()
                    target.execute("VACUUM")
                finally:
                    target.close()
            with compact.open("rb") as source, gzip.open(destination, "wb", compresslevel=9) as output:
                shutil.copyfileobj(source, output)
        sha256 = hashlib.sha256(destination.read_bytes()).hexdigest()
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "asset": destination.name,
            "size_bytes": destination.stat().st_size,
            "sha256": sha256,
            "study_count": self.connection.execute("SELECT COUNT(*) FROM study").fetchone()[0],
            "analysis_unit_count": self.connection.execute("SELECT COUNT(*) FROM analysis_unit").fetchone()[0],
            "includes_local_decisions": include_local_decisions,
        }
        manifest_path = destination.with_suffix(destination.suffix + ".manifest.json")
        manifest_path.write_text(_json(manifest, indent=2), encoding="utf-8")
        manifest["manifest_path"] = str(manifest_path)
        return manifest


def _json(value: Any, indent: int | None = None) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=indent)


def _db_bool(value: bool | None) -> int | None:
    return None if value is None else int(value)

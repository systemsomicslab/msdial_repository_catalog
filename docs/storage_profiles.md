# Catalog storage profiles

The catalog separates three concerns that should not be distributed in the
same way:

1. normalized metadata used for local search and MS-DIAL analysis-unit planning;
2. immutable repository response bodies used for provenance and parser replay;
3. user-specific Class proposals, manual overrides, and reanalysis decisions.

## Local full catalog

The writable local SQLite database is the authoritative working catalog.
Repository response JSON is canonicalized, addressed by SHA-256, gzip-compressed,
and stored once in `source_blob`. `study` points to its current
`source_snapshot`; historical snapshots can refer to the same blob without
duplicating it. Sample attributes are read from `sample_attribute`, rather than
from a second JSON copy in `sample`.

This design keeps SQLite as the transactional query engine while removing the
largest duplicated text fields. It does not make the generated database a good
Git object. `catalog-data/` remains ignored.

### Representative compression check

On 2026-08-27, the five largest stored source payloads from each repository in
the development catalog were read through a read-only SQLite connection. Their
combined canonical JSON size was 153,879,234 bytes and deterministic gzip size
was 5,364,733 bytes (3.5%). Per-source ratios were 1.9% for MB-POST, 7.1% for
MetaboBank, 2.3% for MetaboLights, and 6.0% for Metabolomics Workbench. This is
a workload sample rather than a release-size guarantee, but it confirms that
compressed content-addressed storage addresses the dominant duplicate field.

## Thin distribution catalog

The `thin` snapshot profile retains studies, analysis units, publications,
samples, normalized attributes and contexts, raw-file manifests, evidence, and
search indexes. It removes source response bodies and excludes local decisions
unless explicitly requested.

Create one thin catalog:

```bash
msdial-repository-catalog --database catalog.sqlite snapshot catalog-thin.sqlite.gz --profile thin
```

Create repository shards and `catalog-release-manifest.json`:

```bash
msdial-repository-catalog --database catalog.sqlite release-bundle dist/catalog-release
```

Repository shards make downloads replaceable independently and avoid a single
ever-growing release asset. `--include-provenance` also writes full per-source
assets when an external archive is desired. Large generated assets belong in a
release/object store, not in Git history.

## Existing database migration

Schema migration from version 1 to 2 is additive and fast: it creates the blob
table and reference columns. It deliberately does not rewrite every existing
study during application startup. New or refreshed studies immediately use the
compact layout.

After all catalog writers have stopped, migrate legacy payload copies:

```bash
msdial-repository-catalog --database catalog.sqlite storage-report
msdial-repository-catalog --database catalog.sqlite compact-storage
msdial-repository-catalog --database catalog.sqlite compact-storage --vacuum
```

The first compact pass is resumable because source blobs use `INSERT OR IGNORE`
and snapshots are updated in batches. `--vacuum` reclaims free pages and needs
additional temporary disk space. Back up the database first and never run it
against a GUI or scheduler process that is still writing.

## Update scopes

- `indexed`: refresh only locally known accessions.
- `unindexed`: discover public IDs, subtract local IDs, then apply the limit.
- `discover`: discover and re-read all IDs.

For initial catalog construction, repeated `unindexed --limit N` jobs provide
bounded progress. A periodic `indexed` job detects metadata changes. A less
frequent `discover` job provides a complete audit when time and service limits
permit.

## Git and user overlays

Git contains code, schemas, migrations, documentation, and small release
manifests. It does not contain generated SQLite/WAL files. Thin catalog assets
can be published separately. User decisions remain local and are omitted from
standard snapshots so replacing a downloaded catalog does not publish or
overwrite analysis-specific judgments.

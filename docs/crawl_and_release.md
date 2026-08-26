# Crawl and release operations

## Initial population

1. Run a shallow discovery sweep for all four repositories.
2. Hydrate metadata in bounded batches with a persistent crawl log.
3. Prioritize mixed-method studies and records with multiple assays/analyses.
4. Review a stratified Golden metadata set before publishing classification rules.
5. Publish a versioned catalog snapshot only after validation passes.

Raw mass-spectrometry data are not part of this crawl. Small repository metadata
tables and file manifests may be downloaded and hashed.

## Incremental updates

Each adapter should use a repository update timestamp, ETag, or Last-Modified
header when reliable. Source payload hashes are the fallback and the audit key.
Missing accessions are tombstoned after repeated confirmation rather than
immediately deleted.

## Release assets

Each release contains:

- `msdial-repository-catalog.sqlite.gz`
- `msdial-repository-catalog.sqlite.gz.manifest.json`
- schema/version release notes
- classification and vocabulary change summary

The Interactive app downloads a selected catalog release, verifies SHA-256,
and keeps user review decisions in a separate local overlay.

Public snapshots exclude local Class proposals and manual overrides by default.
Use `--include-local-decisions` only for a private archival snapshot.

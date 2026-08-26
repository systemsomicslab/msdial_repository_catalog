# Architecture

## Responsibility boundary

The catalog is independent of MS-DIAL Interactive and the MS-DIAL Console.

- The catalog crawler discovers, retrieves, normalizes, versions, and searches metadata.
- MS-DIAL Interactive provides human review, raw-data download, parameter setup, and execution.
- MCP agents search the same local catalog and propose purpose-specific analysis decisions.
- MS-DIAL remains responsible for mass-spectrometry data processing.

This keeps repository API changes out of the C# processing engine and prevents
the Interactive UI from becoming the only way to use the metadata.

## Source, normalized, and inferred layers

1. **Source layer** preserves repository payloads, source URLs, retrieval time,
   parser version, and payload hash.
2. **Normalized layer** represents studies, analysis units, samples, files,
   publications, and repository attributes.
3. **Inference layer** stores technical classification, biological context,
   confidence, evidence, review status, and agent decisions.
4. **Analysis layer** stores versioned Class/contrast proposals and their status.

An inference never overwrites its source value.

## Crawl stages

The crawler is intentionally staged:

1. Discovery obtains accession IDs and inexpensive summary fields.
2. Hydration obtains detailed sample/assay metadata without raw data.
3. Unitization splits a study into MS-DIAL-compatible analysis units.
4. Normalization maps repository-specific fields while preserving originals.
5. Review queues ambiguous polarity, acquisition, chromatography, and sample mappings.
6. Publication creates a compressed SQLite snapshot and checksum manifest.

Repositories without reliable revision history are refreshed by content hash.
Failed records are logged independently so that one malformed public record does
not terminate a crawl.

## Analysis unit identity

The preferred source subrecord is repository-native:

- Metabolomics Workbench: `analysis_id`
- MetaboLights: assay file / assay identifier
- MB-POST: analytical-condition preset plus compatible raw-file group
- MetaboBank: SDRF assay/protocol/file group

The stable unit ID combines repository, accession, source subrecord, and the
normalized technical signature. A changed signature creates a new unit instead
of silently rewriting an incompatible historical unit.

## Distribution

Schema, crawler code, tests, and curated overrides belong in Git. Generated
SQLite snapshots should be attached to versioned GitHub Releases together with
a manifest and SHA-256 hash. A local accepted-decision overlay is kept separate
from the downloadable generated snapshot.

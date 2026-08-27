# Architecture

## Responsibility boundary

The catalog is independent of MS-DIAL Interactive and the MS-DIAL Console.

- The catalog crawler discovers, retrieves, normalizes, versions, and searches metadata.
- MS-DIAL Interactive provides human review, raw-data download, parameter setup, and execution.
- MCP agents search the same local catalog and propose purpose-specific analysis decisions.
- MS-DIAL remains responsible for mass-spectrometry data processing.
- The local catalog GUI visualizes the same SQLite data used by CLI and MCP; it
  does not introduce another metadata store.

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

### Repository-specific unitization

- **Metabolomics Workbench** uses `analysis_id`. Factors are often study-level;
  mixed-analysis studies therefore remain in review until sample/file linkage
  is confirmed.
- **MetaboLights** uses each ISA assay file as a unit. Sample rows and spectral
  file references are read from that assay, while biological characteristics
  are joined from the study table and ISA materials.
- **MB-POST** groups raw files by the complete technical signature derived from
  each file's `analyticalCondition` preset. Sample and preparation presets stay
  attached to the corresponding file/sample.
- **MetaboBank** groups SDRF rows by separation, chromatography, polarity,
  acquisition, mobility, instrument, and omics. Vendor directories and SCIEX
  sidecars are expanded from the repository file list.

Unknown DDA/DIA/AIF status is intentionally preserved as `Unknown`. A later raw
header inspection may confirm it, but repository prose alone is not treated as
stronger evidence than a declared assay field.

## Distribution

Schema, crawler code, tests, and curated overrides belong in Git. Generated
SQLite snapshots should be attached to versioned GitHub Releases together with
a manifest and SHA-256 hash. A local accepted-decision overlay is kept separate
from the downloadable generated snapshot.

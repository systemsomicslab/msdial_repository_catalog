# MS-DIAL Repository Metadata Catalog

MS-DIAL Repository Metadata Catalog is a local, provenance-aware index of
public metabolomics studies. It is designed for two connected tasks:

1. Find technically compatible public data for reproducible MS-DIAL reanalysis.
2. Preserve biological context and experimental design for a future metabolite
   contextome.

The catalog stores metadata, file references, and checksums. It does not mirror
all raw mass-spectrometry data.

## Why analysis units matter

A repository accession is a publication/deposition boundary, not necessarily a
single MS-DIAL run. One accession may contain GC-MS, LC-MS positive, LC-MS
negative, DDA, DIA, and other assays. The catalog therefore uses this hierarchy:

```text
Repository -> Study/accession -> Analysis unit -> Sample -> Raw file group
```

An **analysis unit** is the smallest set that can be passed to one MS-DIAL run
without mixing incompatible project type, polarity, acquisition mode, ion
mobility, or chromatography settings.

## Biological context

Repository fields are preserved verbatim. Derived context assertions are stored
separately with source field, source value, method, and confidence. Initial
categories include species, organ/tissue, cell type, disease, inflammation,
aging, sex, genotype, intervention, time, condition, and sample role.

Study-level topics such as "aging" in a title are not silently copied to every
sample. They remain lower-confidence study-topic evidence until reviewed.

## MS-DIAL Class proposals

MS-DIAL supports one `Class` value, while repository metadata may contain age,
sex, region, genotype, intervention, batch, pairing, and other dimensions.
Class is therefore modeled as a versioned analysis decision rather than a fixed
catalog property.

An agent receives the analysis purpose, candidate fields, complete sample
metadata, and an output schema. It returns one Class assignment per sample,
selected fields, a rationale, and an explicit contrast. Proposals must be
validated and can be accepted or replaced without changing source metadata.

## Local usage

No database server or third-party Python dependency is required.

```powershell
python -m pip install -e .

msdial-repository-catalog --database catalog-data/catalog.sqlite init

msdial-repository-catalog --database catalog-data/catalog.sqlite ingest-json `
  examples/mixed-study.json

msdial-repository-catalog --database catalog-data/catalog.sqlite search `
  --separation LC-MS --chromatography "Reversed phase" `
  --ion-mode Negative --acquisition-mode DDA `
  --target-omics Lipidomics --biological-context aging
```

Create a release-ready compressed SQLite snapshot:

```powershell
msdial-repository-catalog --database catalog-data/catalog.sqlite snapshot `
  dist/msdial-repository-catalog.sqlite.gz
```

## Claude/Codex MCP

Install the optional MCP dependency and start the catalog server:

```powershell
python -m pip install -e ".[mcp]"
$env:MSDIAL_REPOSITORY_CATALOG="D:\MSDIAL_Catalog\catalog.sqlite"
msdial-repository-catalog-mcp
```

The catalog MCP searches local metadata and creates an analysis-unit handoff.
MS-DIAL Interactive remains responsible for confirmed raw-data download and
analysis execution.

## Repository adapters

The first milestone accepts normalized JSON produced by the existing MS-DIAL
Interactive adapters. Repository-native crawlers are introduced behind the
`RepositoryAdapter` protocol so that Metabolomics Workbench, MetaboLights,
MB-POST, and MetaboBank can be migrated independently.

During migration, the validated Interactive adapters can populate the catalog:

```powershell
msdial-repository-catalog --database catalog-data/catalog.sqlite `
  crawl-interactive mb_post --accession MPST000007 `
  --interactive-app-root D:\0_SourceCode\msdial_interactive_app
```

Records imported through this bridge retain a warning when the old adapter
collapsed an accession into one analysis unit. They are searchable, but must not
be treated as reviewed mixed-method classifications.

See [Architecture](docs/architecture.md), [Schema](docs/schema.md), and
[Agent Class contract](docs/agent_class_contract.md). The future response layer
is described in [Metabolite contextome model](docs/contextome_model.md).

## License

GNU Lesser General Public License v3.0 or later.

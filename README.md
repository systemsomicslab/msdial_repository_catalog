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

## Local GUI

Launch the cross-platform catalog browser after installing the package:

```powershell
python -m msdial_repository_catalog.gui_server `
  --database catalog-data/catalog.sqlite
```

The browser opens at `http://127.0.0.1:8770/`. Search reads the local SQLite
catalog only and does not use an LLM. The catalog-maintenance panel can contact
public repository metadata services when the user explicitly starts an update;
it never downloads mass-spectrometry raw data. The GUI provides:

- catalog and repository coverage summaries;
- combined technical and biological-context filters;
- Analysis Unit results rather than accession-only results;
- review warnings and repository provenance;
- sample metadata and raw-file manifest previews.
- indexed refresh, unindexed-only growth, and full discovery crawls with progress, ETA, and cancellation.

The routine `Refresh indexed accessions` scope re-fetches only studies already
present in the local catalog. `Discover and fetch unindexed accessions only`
subtracts local accessions before applying the optional limit, so repeated
bounded runs grow the catalog without re-reading completed records. `Discover
new and refresh all` re-checks every public record and can therefore take hours.
Only one update runs at a time, and repositories are processed sequentially.

Use `--no-browser` when starting it from an agent or service, and set a different
port with `--port`. The default database is
`~/.msdial/repository-catalog.sqlite`, shared with the MCP server.

When the Python Scripts directory is on `PATH`, the equivalent installed command
is `msdial-repository-catalog-gui`.

From a source checkout, Windows users can also double-click
`scripts/start-gui-windows.cmd`. On macOS or Linux:

```bash
chmod +x scripts/start-gui.sh
./scripts/start-gui.sh
```

Both source-checkout launchers use `catalog-data/catalog.sqlite` unless
`MSDIAL_REPOSITORY_CATALOG` is set.

Create a release-ready thin SQLite snapshot. Thin catalogs retain searchable
normalized metadata but omit archived repository response bodies and local
Class decisions:

```powershell
msdial-repository-catalog --database catalog-data/catalog.sqlite snapshot `
  dist/msdial-repository-catalog.sqlite.gz --profile thin
```

Generate one thin asset per repository and an aggregate release manifest:

```powershell
msdial-repository-catalog --database catalog-data/catalog.sqlite `
  release-bundle dist/catalog-release
```

Source response bodies are gzip-compressed and deduplicated by SHA-256 inside a
local full catalog. Existing schema-1 databases can be migrated after all GUI,
CLI, and scheduler writers have stopped:

```powershell
msdial-repository-catalog --database catalog-data/catalog.sqlite storage-report
msdial-repository-catalog --database catalog-data/catalog.sqlite compact-storage --vacuum
```

Do not commit generated SQLite files to Git. See
[Storage profiles](docs/storage_profiles.md) for the distribution architecture,
migration procedure, and separation between shareable catalog data and local
analysis decisions.

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

Agents can also start, observe, and cancel catalog metadata updates with
`msdial_catalog_update_start`, `msdial_catalog_update_status`, and
`msdial_catalog_update_cancel`. Starting requires explicit confirmation because
it contacts public services. See [Scheduled updates](docs/scheduled_updates.md).

## Repository adapters

The catalog includes independent native metadata adapters for Metabolomics
Workbench, MetaboLights, MB-POST, and MetaboBank. They retrieve repository
metadata and file manifests only; they do not download raw mass-spectrometry
data.

Hydrate selected accessions directly into the local catalog:

```powershell
msdial-repository-catalog --database catalog-data/catalog.sqlite `
  crawl metabolomics_workbench --accession ST000941

msdial-repository-catalog --database catalog-data/catalog.sqlite `
  crawl metabolights --accession MTBLS341

msdial-repository-catalog --database catalog-data/catalog.sqlite `
  crawl mb_post --accession MPST000007

msdial-repository-catalog --database catalog-data/catalog.sqlite `
  crawl metabobank --accession MTBKS47
```

Omit `--accession` to crawl discovered records, and use `--limit` for a bounded
development run. A public record failure is logged without terminating the
remaining crawl.

Unitization follows repository-native boundaries:

- Metabolomics Workbench: one unit per `analysis_id`
- MetaboLights: one unit per assay file
- MB-POST: one unit per compatible `analyticalCondition` preset signature
- MetaboBank: one unit per distinct SDRF technical signature and file group

When sample-to-analysis or archive-to-analysis linkage is not declared, the
adapter retains the source references and marks the unit `needs_review`. It does
not silently invent a definitive assignment. See
[Repository adapters](docs/repository_adapters.md) for current limitations and
validation records.

The original MS-DIAL Interactive bridge remains available for compatibility:

```powershell
msdial-repository-catalog --database catalog-data/catalog.sqlite `
  crawl-interactive mb_post --accession MPST000007 `
  --interactive-app-root D:\0_SourceCode\msdial_interactive_app
```

Records imported through this bridge retain a warning when the old adapter
collapsed an accession into one analysis unit. New catalog builds should use
the native `crawl` command.

See [Architecture](docs/architecture.md), [Schema](docs/schema.md),
[Storage profiles](docs/storage_profiles.md), and
[Agent Class contract](docs/agent_class_contract.md). The future response layer
is described in [Metabolite contextome model](docs/contextome_model.md).

## License

GNU Lesser General Public License v3.0 or later.

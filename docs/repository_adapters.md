# Repository adapters

## Scope

Native adapters retrieve public metadata, assay/sample tables, and download
manifests. They do not download raw data. Every response used for normalization
is preserved in `source_snapshot` through the study source payload.

## Metabolomics Workbench

`analysis_id` is the unit boundary. Declared analysis polarity,
chromatography, and instrument fields take precedence over study prose. The
factors endpoint is frequently study-level and does not always identify which
analysis owns a sample. For mixed studies, factors and shared archives are
retained with a review warning.

Live validation: `ST000941` produced `AN001543` (normal-phase positive LC-MS)
and `AN001544` (normal-phase negative LC-MS). That record exposes processed
result files but no raw archive on its download page.

## MetaboLights

Each ISA assay file is one analysis unit. Technical fields, samples, and raw or
derived spectral references come from the assay. Study material and sample
table fields add biological context without changing the assay boundary.

Live validation: `MTBLS341` produced eight units: two GC-MS assays and six
LC-MS assays separated by tissue/material and positive/negative polarity.

## MB-POST

Every primary raw file has detailed preset metadata. Files are grouped by the
normalized `analyticalCondition` signature. Different polarity, acquisition,
chromatography, mobility, instrument, or target-omics values therefore create
different units even inside one project accession.

Live validation: `MPST000007` produced one 30-sample, negative DDA,
reversed-phase lipidomics unit. Sample, preparation, software, and analytical
condition presets remain available as verbatim sample attributes.

## MetaboBank

SDRF rows are grouped by their technical signature. Raw references are resolved
against the repository file list, including vendor-directory members and SCIEX
sidecars. ABF is used only when the SDRF provides no original vendor reference,
and that fallback is reported.

A vendor directory is stored as its member files, one row each, because each
has its own URL and MD5. The analysis-unit view groups them again: every file
under a `.raw` or `.d` segment is a member of that folder, and the folder is one
analysis input matched to the SDRF row that names it (`raw/x.raw/`).

Live validation: `MTBKS47` exposed polarity-switching LC-MS SDRF rows and a
large Waters directory-file manifest. Its metadata also contains conflicting
`untargeted` and `widely targeted` wording, so untargeted status remains unknown
and the unit requires review.

## Ion mobility

A unit is ion mobility only on its own evidence (decided 2026-10-03): an
instrument field naming a mobility instrument (timsTOF, Synapt, Vion, Agilent
6560, Cyclic IMS, HDMS), a field about ion mobility that says it was on or off,
or a container that holds mobility data. A field that says mobility was not
used names the technique too, and is read as off: "No ion mobility", "TIMS
off", "Ion mobility not used", "DDA without ion mobility". Study-level text --
a title, abstract, description or protocol shared by every unit of the study --
is never evidence.
MetaboBank MTBKS217 (a Waters Xevo G2 QTOF unit) was stored `Enabled` only
because the lipidome-atlas abstract it shares with its sibling accessions
mentions ion mobility.

The stored `ion_mobility` column is not rewritten: it is part of the technical
signature, so a corrected value would return each changed unit under a new
`unit_id` and the upsert cascade would delete the old one with its Class
proposals and run records. `ion_mobility.ion_mobility_evidence(unit)` projects
the unit's view instead, onto `enabled`, `mixed`, `none` or `unknown`, with the
source that decided it (`row_instrument`, `assay_parameter`,
`container_format`, or `study_text` for an unknown state), and the instruments
and container formats seen. `mixed` is mobility evidence beside containers that
hold none, as in MTBKS219/220, whose Bruker BAF folders sit beside TDF folders;
Interactive's per-file header check and split exclude the mobility part.
`Catalog.get_unit` carries the projection as `ion_mobility_evidence`, and the
handoff's `technical_settings.ion_mobility` is its value (`Enabled`, `Mixed`,
`Disabled`, `Unknown`).

The adapters keep storing the crawl's reading for the same reason: a crawl
re-ingests a study whose payload hash moved, so adapters that stored the
corrected value would re-key 350 units of the 2026-10-03 catalog, the pilot's
MTBKS217 negative among them, at the first routine update after a campaign
releases its lock. Storing the corrected value waits until a re-crawl keeps unit
identity, by carrying proposals and run records over to the re-keyed unit or by
keeping `ion_mobility` out of the `unit_id` and the source subrecord id.

## Review policy

The adapters never convert ambiguity into a reviewed fact. Typical review
triggers are:

- unknown LC-MS DDA/DIA/AIF mode;
- unknown or combined polarity requiring raw-header inspection or splitting;
- study-level samples that cannot be mapped uniquely to an analysis;
- shared archive files with no declared analysis ownership;
- conflicting targeted/untargeted descriptions;
- missing or unresolvable raw-data references.

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

Live validation: `MTBKS47` exposed polarity-switching LC-MS SDRF rows and a
large Waters directory-file manifest. Its metadata also contains conflicting
`untargeted` and `widely targeted` wording, so untargeted status remains unknown
and the unit requires review.

## Review policy

The adapters never convert ambiguity into a reviewed fact. Typical review
triggers are:

- unknown LC-MS DDA/DIA/AIF mode;
- unknown or combined polarity requiring raw-header inspection or splitting;
- study-level samples that cannot be mapped uniquely to an analysis;
- shared archive files with no declared analysis ownership;
- conflicting targeted/untargeted descriptions;
- missing or unresolvable raw-data references.

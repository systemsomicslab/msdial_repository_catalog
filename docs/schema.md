# Catalog schema

## Core entities

| Entity | Purpose |
| --- | --- |
| `study` | Repository accession, publication boundary, source payload hash |
| `source_snapshot` | Immutable source payload versions keyed by hash and parser version |
| `analysis_unit` | One technically compatible MS-DIAL run candidate |
| `sample` | Biological or analytical sample within an analysis unit |
| `raw_file` | Raw file, vendor directory member, or required sidecar |
| `sample_attribute` | Verbatim repository field/value |
| `sample_context` | Derived context with evidence and confidence |
| `analysis_unit_context` | Study/assay-level topics that must not be assigned to each sample |
| `publication` | DOI, PubMed ID, and citation title |
| `class_proposal` | Purpose-specific grouping and contrast decision |
| `class_assignment` | One proposed MS-DIAL Class value per sample |
| `crawl_run` | Incremental update provenance and failures |
| `manual_override` | Reviewed local correction without mutating repository source |
| `analysis_run` | Reanalysis provenance, software versions, parameter hash, mzTab-M checksum |
| `contrast` | Explicit case/control/covariate definition for one analysis purpose |
| `metabolite_entity` | Cross-study chemical identity and structural identifiers |
| `metabolite_observation` | Dataset-specific MS-DIAL feature and annotation evidence |
| `metabolite_response` | Effect direction and statistics for one contrast |

## Technical signature

Each analysis unit records:

- separation
- chromatography
- ion mode
- acquisition mode
- ion mobility
- instrument
- target omics
- untargeted status

Unknown values remain `Unknown`; they are not treated as wildcards when units
are combined.

## Context vocabulary

The first vocabulary covers the dimensions needed for repository reanalysis and
the metabolite contextome:

- Sample: species, organ/tissue, cell type, age, sex
- Condition: disease, inflammation, genotype, intervention, nutrition, stress, time
- Design: condition/group, sample role, batch, pairing, analytical order
- Topic/archetype evidence: aging, inflammation, senescence, stress,
  development, recovery, nutrition, disease

Ontology identifiers are optional in schema v1. Later vocabulary releases can
add ontology mappings without changing source values.

## Contextome result boundary

Three evidence levels are deliberately separated:

1. `analysis_unit_context`: a study or assay is about aging or inflammation.
2. `sample_context`: an individual sample is assigned an age, disease, tissue,
   intervention, or other context by repository metadata or review.
3. `metabolite_response`: a reanalysis contrast estimated an effect for a
   feature/metabolite.

A title keyword is searchable but cannot become a case/control assignment or a
metabolite response without additional evidence.

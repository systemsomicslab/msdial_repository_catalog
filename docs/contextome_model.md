# Metabolite contextome data model

The infrastructure follows four evidence stages:

```text
Observation -> Context -> Prediction -> Causality
```

## Observation

An MS-DIAL run produces features, annotations, abundance matrices, mzTab-M,
quality-assurance evidence, and reproducible parameters. Each observation keeps
its dataset-specific feature ID even when no chemical identity is available.

## Context

Repository and reviewed metadata describe:

- Sample: species, organ/tissue, cell type, age, sex
- Condition: disease, inflammation, drug/diet, gene perturbation, time
- Design: control/treatment, replicate, pairing, batch, analytical order
- MS/annotation: chromatography, polarity, acquisition, library, confidence

Context is attached at the narrowest defensible level. Study topics, sample
assignments, and contrast definitions remain distinct.

## Response

A response links one metabolite observation to an explicit contrast and stores
direction, fold change, effect size, uncertainty, group size, and statistical
method. This supports cross-study questions such as shared aging,
inflammation, stress, recovery, development, or nutrition responses.

## Prediction and causality

Response archetypes and candidate rankings are derived products, not source
metadata. Genetics, mechanism, tractability, structure confidence, and deep
cell/tissue context can each contribute evidence. They must not be collapsed
into a single causal claim.

Wet validation evidence can later be connected as a separate evidence type for
add/inhibit/KD/OE/rescue experiments. Until then, repository reanalysis remains
observational.

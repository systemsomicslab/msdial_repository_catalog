"""Whether an analysis unit is ion mobility, read from the unit's own evidence.

WHAT THIS ENDS. A unit's ion_mobility column was inferred from text that included the study's: a
MetaboBank unit read its study title, description and protocol properties beside its own SDRF rows,
and a Metabolomics Workbench unit read the study summary and the HTML detail page beside its own
analysis record. MetaboBank MTBKS217 is a Waters Xevo G2 QTOF unit, and it was stored Enabled because
the MS-DIAL 4 lipidome-atlas abstract it shares with its sibling accessions mentions ion mobility; the
campaign plan then excluded it, and MTBKS218 and MTBKS221 with it, as LC-IM-MS. The user decided on
2026-10-03 (option A) that a unit counts as ion mobility only on unit-level evidence, and that a unit
whose mobility data sit beside data without mobility -- MTBKS219 and MTBKS220 list Bruker BAF folders
(timsOFF) beside TDF folders (PASEF) -- is not excluded at plan time: Interactive's per-file header
check and split exclude the mobility files.

WHAT IT READS. Only what the Catalog's view of the unit holds (Catalog.get_unit):

- row_instrument: the unit's instrument column, or an instrument field of one of its sample rows,
  naming a mobility instrument (adapters.common.ION_MOBILITY_INSTRUMENT), unless the same field says
  mobility was off ("timsTOF Pro, TIMS off");
- assay_parameter: a row field about ion mobility that says it was on or off, or an acquisition field
  that names a mobility acquisition (PASEF, HDMSE) or says there was none ("DDA without ion
  mobility"). A statement that mobility was not used names the technique too, so it is read first
  (adapters.common.ion_mobility_negated): "No ion mobility" or "TIMS off" is off, never on;
- container_format: an analysis input whose container holds mobility data -- a Bruker TDF folder, a
  Waters folder with _FUNCnnn.CDT drift files, an Agilent folder with AcqData/IMSFrame.bin, the same
  readings Interactive makes on disk -- or one that cannot, a Bruker BAF or TSF folder;
- study_text: the study's title and description, or the adapter's warning that they mention ion
  mobility, or a stored Enabled that nothing of the unit's own supports, which the crawl can only
  have read from the study text beside it. This is never evidence about the unit. It is reported,
  as the source of an unknown state, so that a reader can see why a stored Enabled no longer holds.

THE STATES. enabled: mobility evidence and nothing beside it. mixed: mobility evidence beside
containers that hold none, beside rows that say mobility was off, or on only some of the unit's rows,
so part of the unit is not ion mobility; MTBKS219/220 are mixed. none: a parameter says mobility was
off, or every input is a container that cannot hold it. unknown: the unit's own evidence says
nothing.

WHY THE STORED COLUMN IS LEFT AS IT IS. The live Catalog stores ion_mobility per unit from the crawl,
and a re-parse from the stored payloads would put the corrected value there -- but ion_mobility is
part of the technical signature, and a unit_id is stable_id(repository, accession, source subrecord,
technical signature); a MetaboBank source subrecord id is a hash of that signature too. Every unit
whose value changed would come back under a new unit_id, and the upsert cascade (Catalog.ingest_study)
deletes the old one with its Class proposals and analysis-run records, and strands the handoffs,
workspaces and campaign manifests that name it; the 2026-10-03 pilot names MTBKS217 negative by its
unit_id. A MetaboBank re-parse would also move more than this field: the payload is stored as JSON
with sorted keys, so the SDRF column order that picks a unit's instrument field is gone, and MTBKS222
would come back with other instruments. So the stored column keeps the crawl's reading, and the
handoff's technical_settings and the campaign runner read this projection instead.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Iterable, Mapping

from .adapters.common import (
    ION_MOBILITY_INSTRUMENT,
    ION_MOBILITY_STUDY_TEXT_CODE,
    ION_MOBILITY_STUDY_TEXT_WARNING,
    declared_ion_mobility,
    ion_mobility_negated,
    metadata_scalar,
    study_mentions_ion_mobility,
)

ION_MOBILITY_EVIDENCE_SCHEMA = "msdial-catalog-ion-mobility-evidence.v1"
STATES: tuple[str, ...] = ("enabled", "mixed", "none", "unknown")
# In order of precedence: when several say mobility was used, the first names the source.
SOURCES: tuple[str, ...] = ("row_instrument", "assay_parameter", "container_format", "study_text")

# Interactive's names for the containers that carry a mobility dimension (raw_metadata_preflight.
# ION_MOBILITY_FORMATS) and for the Bruker containers that cannot: a BAF or TSF folder is a timsTOF
# acquisition with TIMS off. A Waters or Agilent folder without its drift files says nothing either
# way here, because a listing need not enumerate every member.
ION_MOBILITY_CONTAINER_FORMATS = frozenset({"bruker_tdf", "waters_raw_im", "agilent_d_im"})
NON_ION_MOBILITY_CONTAINER_FORMATS = frozenset({"bruker_baf", "bruker_tsf"})

ION_MOBILITY_STORED_WITHOUT_EVIDENCE_CODE = "ion_mobility_stored_without_unit_evidence"
ION_MOBILITY_STORED_WITHOUT_EVIDENCE_WARNING = (
    f"{ION_MOBILITY_STORED_WITHOUT_EVIDENCE_CODE}: the stored ion_mobility Enabled was read from "
    "study-level text the crawl saw beside this unit's own; none of the unit's own rows, fields or "
    "containers says ion mobility, so it is Unknown here and the raw headers settle it."
)

# The value the handoff's technical_settings carries for each state, in the column's vocabulary.
TECHNICAL_SETTING = {"enabled": "Enabled", "mixed": "Mixed", "none": "Disabled", "unknown": "Unknown"}

_ACQUISITION_FIELDS = ("acquisition", "instrument mode", "scan mode", "scan type")
_MOBILITY_ACQUISITION = re.compile(r"pasef|\bhdmse?\b|\bion[\s-]+mobility\b", re.IGNORECASE)
_WATERS_DRIFT_FILE = re.compile(r"_func\d+\.cdt")
_AGILENT_DRIFT_FILE = "acqdata/imsframe.bin"


def ion_mobility_evidence(unit: Mapping[str, Any]) -> dict[str, Any]:
    """Project a unit onto its ion-mobility state, from unit-level evidence only.

    `unit` is the view Catalog.get_unit returns: its instrument column, its sample rows with their
    attributes, its files and analysis_inputs, and the study title and description. A unit without
    analysis_inputs has them projected from its files and rows. Returns the state, the source that
    decided it, every source that said anything, the instruments and container formats seen, and
    the warnings a reader of the unit should see.
    """
    rows = [_row_attributes(row) for row in unit.get("samples") or []]
    unit_instrument = metadata_scalar(unit.get("instrument"))
    unit_instrument_im = _names_ion_mobility_instrument(unit_instrument)

    instruments: Counter[str] = Counter()
    if unit_instrument:
        instruments[unit_instrument] += 1
    rows_on = rows_off = rows_named = rows_with_evidence = 0
    for attributes in rows:
        reading = _read_row(attributes)
        instruments.update(reading["instruments"])
        rows_on += reading["on"]
        rows_off += reading["off"]
        rows_named += reading["named"]
        rows_with_evidence += reading["on"] or reading["named"]

    containers = _container_reading(unit)

    positive = []
    if unit_instrument_im or rows_named:
        positive.append("row_instrument")
    if rows_on:
        positive.append("assay_parameter")
    if containers["ion_mobility"]:
        positive.append("container_format")
    negative = []
    if rows_off:
        negative.append("assay_parameter")
    if containers["non_ion_mobility"]:
        negative.append("container_format")
    partial = bool(rows) and 0 < rows_with_evidence < len(rows) and not unit_instrument_im

    stored = str(unit.get("ion_mobility") or "Unknown")
    mentioned = study_mentions_ion_mobility(unit.get("title"), unit.get("description")) or any(
        str(item).startswith(ION_MOBILITY_STUDY_TEXT_CODE) for item in unit.get("warnings") or []
    )
    warnings: list[str] = []
    if positive:
        state = "mixed" if negative or partial else "enabled"
        source: str | None = positive[0]
    elif "assay_parameter" in negative or (
        containers["non_ion_mobility"] and containers["non_ion_mobility"] == containers["inputs"]
    ):
        state, source = "none", negative[0]
    else:
        state, source = "unknown", None
        if mentioned:
            source = "study_text"
            warnings.append(ION_MOBILITY_STUDY_TEXT_WARNING)
        elif stored == "Enabled":
            # The crawl read study text the unit's view does not carry -- a Workbench detail page,
            # MetaboBank protocol properties -- and stored Enabled on it. MTBKS22 is a GC-MS unit
            # stored Enabled because its LED panels came from "CCS Co.".
            source = "study_text"
            warnings.append(ION_MOBILITY_STORED_WITHOUT_EVIDENCE_WARNING)

    sources = [
        name
        for name in SOURCES
        if name in positive or name in negative or (name == "study_text" and source == name)
    ]
    return {
        "schema": ION_MOBILITY_EVIDENCE_SCHEMA,
        "state": state,
        "source": source,
        "sources": sources,
        "technical_setting": TECHNICAL_SETTING[state],
        "stored_ion_mobility": stored,
        "instruments": sorted(instruments),
        "ion_mobility_instruments": sorted(
            name for name in instruments if _names_ion_mobility_instrument(name)
        ),
        "container_formats": dict(sorted(containers["formats"].items())),
        "ion_mobility_container_count": containers["ion_mobility"],
        "non_ion_mobility_container_count": containers["non_ion_mobility"],
        "analysis_input_count": containers["inputs"],
        "row_count": len(rows),
        "rows_with_ion_mobility_evidence": rows_with_evidence,
        "rows_saying_ion_mobility_off": rows_off,
        "study_text_mentions_ion_mobility": mentioned,
        "reason": _reason(state, source, positive, negative, partial, containers),
        "warnings": warnings,
    }


def _row_attributes(row: Mapping[str, Any]) -> dict[str, str]:
    values = row.get("attributes")
    if values is None:
        values = row.get("values")
    return {str(key): metadata_scalar(value) for key, value in dict(values or {}).items()}


def _read_row(attributes: Mapping[str, str]) -> dict[str, Any]:
    """One sample row: the instruments it names, and whether it says mobility was on or off."""
    on = off = named = False
    instruments: list[str] = []
    for key, value in attributes.items():
        field, text = key.casefold(), value.strip()
        if not text:
            continue
        if "mobility" in field:
            declared = declared_ion_mobility(text)
            on = on or declared == "Enabled"
            off = off or declared == "Disabled"
        elif any(token in field for token in _ACQUISITION_FIELDS):
            # "DDA without ion mobility" names the technique and says it was off.
            negated = ion_mobility_negated(text)
            off = off or negated
            on = on or (not negated and bool(_MOBILITY_ACQUISITION.search(text)))
        elif "instrument" in field:
            instruments.append(text)
            named = named or _names_ion_mobility_instrument(text)
    return {"on": on and not off, "off": off and not on, "named": named, "instruments": instruments}


def _names_ion_mobility_instrument(text: str) -> bool:
    """An instrument field naming a mobility instrument, unless it says mobility was off."""
    return bool(ION_MOBILITY_INSTRUMENT.search(text)) and not ion_mobility_negated(text)


def _container_reading(unit: Mapping[str, Any]) -> dict[str, Any]:
    """The analysis inputs' container formats, and how many carry mobility data or cannot."""
    inputs = unit.get("analysis_inputs")
    files = list(unit.get("files") or [])
    if inputs is None:
        from .class_proposal import normalize_analysis_unit

        projected = normalize_analysis_unit(dict(unit))
        inputs, files = projected["analysis_inputs"], projected["files"]
    drift = _folders_with_drift_files(files)
    formats: Counter[str] = Counter()
    mobility = without = 0
    for entry in inputs or []:
        tokens = {token for token in str(entry.get("format") or "").split("+") if token}
        path = str(entry.get("path") or "").replace("\\", "/").strip("/").casefold()
        if entry.get("kind") == "vendor_folder" and path in drift:
            # Interactive's name for the same folder read on disk: waters_raw_im, agilent_d_im.
            tokens = (tokens - {"waters_raw", "agilent_d", "unknown"}) | {drift[path]}
        label = "+".join(sorted(tokens)) or str(entry.get("suffix") or "") or "unrecognised"
        formats[label] += 1
        if tokens & ION_MOBILITY_CONTAINER_FORMATS:
            mobility += 1
        elif tokens and tokens <= NON_ION_MOBILITY_CONTAINER_FORMATS:
            without += 1
    return {
        "formats": formats,
        "ion_mobility": mobility,
        "non_ion_mobility": without,
        "inputs": len(inputs or []),
    }


def _folders_with_drift_files(files: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """Vendor folders whose listed members include drift-time data, keyed by folder path."""
    from .class_proposal import container_of

    found: dict[str, str] = {}
    for item in files:
        path = str(item.get("path") or item.get("name") or "").replace("\\", "/").lstrip("/")
        container = str(item.get("container") or "") or container_of(path)
        if not container:
            continue
        member = path[len(container):].lstrip("/").casefold()
        key = container.strip("/").casefold()
        if _WATERS_DRIFT_FILE.fullmatch(member):
            found[key] = "waters_raw_im"
        elif member == _AGILENT_DRIFT_FILE:
            found[key] = "agilent_d_im"
    return found


def _reason(
    state: str,
    source: str | None,
    positive: list[str],
    negative: list[str],
    partial: bool,
    containers: Mapping[str, Any],
) -> str:
    if state == "mixed":
        beside = []
        if "container_format" in negative:
            beside.append(
                f"{containers['non_ion_mobility']} of {containers['inputs']} analysis inputs are "
                "containers that hold no mobility data"
            )
        if "assay_parameter" in negative:
            beside.append("some rows say ion mobility was off")
        if partial:
            beside.append("only some of the unit's rows carry it")
        return (
            f"ion-mobility evidence ({', '.join(positive)}) beside data without it: "
            + "; ".join(beside)
            + ". Interactive's header check and split exclude the mobility inputs."
        )
    if state == "enabled":
        return f"the unit's own evidence says ion mobility ({', '.join(positive)})"
    if state == "none":
        return f"the unit's own evidence says no ion mobility ({', '.join(negative)})"
    if source == "study_text":
        return (
            "only the study's text mentions ion mobility, which is not evidence about this unit; "
            "the raw headers settle it"
        )
    return "nothing in the unit's own rows, fields or containers says whether ion mobility was used"

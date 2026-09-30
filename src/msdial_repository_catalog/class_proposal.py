from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from typing import Any

from .models import ClassAssignment, ClassProposal, stable_id


CLASS_PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["purpose", "selected_fields", "rationale", "assignments"],
    "properties": {
        "purpose": {"type": "string"},
        "selected_fields": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "rationale": {"type": "string", "minLength": 1},
        "contrast_definition": {"type": "object"},
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["sample_id", "class_label", "values"],
                "properties": {
                    "sample_id": {"type": "string"},
                    "class_label": {"type": "string"},
                    "values": {"type": "object"},
                },
            },
        },
    },
}


def build_class_proposal_request(
    unit: dict[str, Any],
    purpose: str,
    *,
    include_samples: bool = False,
    sample_limit: int = 0,
) -> dict[str, Any]:
    samples = analysis_samples(unit)
    analysis_unit = {**unit, "samples": samples}
    fields = candidate_fields(analysis_unit)
    request = {
        "task": "Propose MS-DIAL Class labels and an explicit statistical contrast.",
        "purpose": purpose,
        "analysis_unit": {
            key: unit.get(key)
            for key in (
                "unit_id", "repository", "accession", "title", "label", "separation",
                "chromatography", "ion_mode", "acquisition_mode", "target_omics",
            )
        },
        "candidate_fields": fields,
        "sample_count": len(samples),
        "instructions": [
            "Use biological fields that answer the stated purpose; do not choose fields only because they vary.",
            "Keep Blank, QC, Standard, batch, pairing, and analytical order available as design covariates.",
            "Do not merge unknown values with a biological group.",
            "Join selected values with underscore; normalize underscores inside each value to hyphens.",
            "Return one assignment for every sample and cite the source fields in rationale.",
            "Treat study-level topics as hypotheses, not sample-level assignments.",
        ],
        "output_schema": CLASS_PROPOSAL_SCHEMA,
    }
    if include_samples:
        if sample_limit > 0:
            samples = samples[:sample_limit]
        request["samples"] = [
            {
                "sample_id": sample["sample_id"],
                "raw_file": sample.get("raw_file", ""),
                "attributes": sample.get("attributes", {}),
                "contexts": sample.get("contexts", []),
            }
            for sample in samples
        ]
    return request


def candidate_fields(unit: dict[str, Any]) -> list[dict[str, Any]]:
    samples = unit.get("samples", [])
    total = len(samples)
    values: dict[str, list[str]] = defaultdict(list)
    for sample in samples:
        for field, value in sample.get("attributes", {}).items():
            text = _scalar(value)
            if text:
                values[str(field)].append(text)
    result = []
    for field, items in values.items():
        distinct = sorted(set(items), key=str.casefold)
        score = _field_priority(field) + (len(items) / max(1, total)) * 2
        if len(distinct) == 1:
            score -= 5
        elif len(distinct) <= max(20, total // 2):
            score += 2
        result.append(
            {
                "field": field,
                "present": len(items),
                "total": total,
                "distinct_count": len(distinct),
                "examples": distinct[:8],
                "priority_score": round(score, 3),
            }
        )
    return sorted(result, key=lambda item: (-item["priority_score"], item["field"].casefold()))


# The record of an abstention: no field was usable, so no contrast is defined and every sample
# takes one Class. It is saved and ratified like a proposal, because declining to compare is a
# scientific decision too.
ABSTENTION_KIND = "abstention"
ABSTENTION_LABEL = "All"
ABSTENTION_REASONS = ("no_declared_factor", "no_usable_declared_factor")


def field_based_proposal(
    unit: dict[str, Any],
    purpose: str,
    selected_fields: list[str],
    rationale: str = "Deterministic field-based proposal for review.",
) -> ClassProposal:
    if not selected_fields:
        raise ValueError("At least one metadata field is required.")
    assignments = []
    seen: dict[str, dict[str, str]] = {}
    for sample in analysis_samples(unit):
        attributes = sample.get("attributes", {})
        values = {field: _scalar(attributes.get(field)) or "NA" for field in selected_fields}
        sample_id = str(sample["sample_id"])
        if sample_id in seen:
            # ONE ASSIGNMENT PER SAMPLE, however many inputs it has, as abstention_record counts it:
            # MetaboBank MTBKS219 names each mouse tissue three times, once per .d folder, and a
            # proposal assigning it three times is refused here and by Interactive alike. Rows that
            # disagree about the sample are refused rather than settled by whichever came first.
            if seen[sample_id] != values:
                raise ValueError(
                    f"The rows of sample {sample_id} disagree on {', '.join(selected_fields)}, "
                    "so one Class cannot represent them."
                )
            continue
        seen[sample_id] = values
        label = "_".join(_class_token(values[field]) or "NA" for field in selected_fields)
        assignments.append(ClassAssignment(sample_id=sample_id, class_label=label, values=values))
    payload = json.dumps(
        {"unit_id": unit["unit_id"], "purpose": purpose, "fields": selected_fields, "assignments": [a.class_label for a in assignments]},
        ensure_ascii=False,
        sort_keys=True,
    )
    proposal = ClassProposal(
        proposal_id=stable_id("class-proposal", payload),
        unit_id=str(unit["unit_id"]),
        purpose=purpose,
        selected_fields=selected_fields,
        assignments=assignments,
        rationale=rationale,
        model="deterministic-field-projection",
        prompt_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    )
    validate_class_proposal(unit, proposal)
    return proposal


def validate_class_proposal(unit: dict[str, Any], proposal: ClassProposal) -> None:
    expected = [str(item["sample_id"]) for item in analysis_samples(unit)]
    actual = [item.sample_id for item in proposal.assignments]
    duplicates = [sample for sample, count in Counter(actual).items() if count > 1]
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    problems = []
    if duplicates:
        problems.append(f"duplicate samples: {', '.join(duplicates)}")
    if missing:
        problems.append(f"missing samples: {', '.join(missing)}")
    if extra:
        problems.append(f"unknown samples: {', '.join(extra)}")
    if not proposal.rationale.strip():
        problems.append("rationale is empty")
    abstention = is_abstention(proposal)
    if abstention:
        # An abstention selects no field and puts every sample in the one Class ABSTENTION_LABEL, for
        # a reason the selection gives: exactly what abstention_record writes.
        if proposal.selected_fields:
            problems.append("an abstention selects no field")
        labels = {item.class_label for item in proposal.assignments}
        if labels != {ABSTENTION_LABEL} or proposal.contrast_definition.get("class_label") != ABSTENTION_LABEL:
            problems.append(f"an abstention puts every sample in the one Class {ABSTENTION_LABEL!r}")
        if proposal.contrast_definition.get("reason") not in ABSTENTION_REASONS:
            problems.append(f"an abstention's reason is one of {', '.join(ABSTENTION_REASONS)}")
    elif not proposal.selected_fields:
        problems.append("selected_fields is empty")
    for assignment in proposal.assignments:
        if not assignment.class_label.strip():
            problems.append(f"empty Class for {assignment.sample_id}")
        if "__" in assignment.class_label or assignment.class_label.startswith("_"):
            problems.append(f"invalid Class delimiter for {assignment.sample_id}")
    if problems:
        raise ValueError("Invalid Class proposal: " + "; ".join(problems))


def is_abstention(proposal: ClassProposal) -> bool:
    """Whether the record says no Class was defined: the unit runs with no contrast."""
    return (proposal.contrast_definition or {}).get("kind") == ABSTENTION_KIND


def analysis_samples(unit: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one metadata row per MS-DIAL analysis input.

    A row that names a folder the file listing lacks, or a second row naming one folder, is kept as
    well, and the unit's analysis_input_issues say so. A row naming the directory other rows'
    inputs sit in is dropped, as it always was; the view keeps it in `excluded_samples`, and the
    issues say that too.
    """
    return _project_analysis_inputs(unit)["samples"]


def analysis_inputs(unit: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the unit's analysis inputs: one per file, vendor folder or packed container.

    Each entry names what MS-DIAL opens (`path`, relative to the download root), its `kind`, the
    container `suffix`, the vendor `format` read from a folder's member names, how many listed
    files make it up and their bytes, and the sample row that names it. A file or packed container
    also says whether it `requires_conversion` and its `conversion_target` ("" when none is
    planned).

    THE TOKENS A SPLIT HINT IS KEYED ON. A vendor folder's `format` is UNKNOWN_FORMAT ("unknown")
    when its member names do not say, never "", and split_hint.groups counts it under the same
    token; every other input has format "", which no group ever uses, because only vendor folders
    are split by format. A split by `suffix` counts vendor containers by their suffix (".raw",
    ".wiff"), and a converted or unrecognised input belongs to no group either.
    """
    return _project_analysis_inputs(unit)["inputs"]


# The containers MS-DIAL opens directly, split by how they were produced. A vendor container is
# what the instrument wrote; a converted one is an open re-encoding of it.
#
# WHICH ONE WINS, AND WHY. When a repository publishes both for the same sample -- MetaboBank
# MTBKS157 publishes every one of its sixteen samples as .RAW and again as .mzXML -- exactly one
# may be analysed, or the sample is measured twice and aligned against itself. The vendor
# container is preferred, on the analyst's instruction of 2026-09-21: its readers are the more
# stable of the two in practice.
# Exactly the formats MS-DIAL opens, from SupportMsRawDataExtension in
# MsdialCore/Enum/SupportFormat.cs: abf, ibf, cdf, mzml, wiff, raw, d, wiff2, qgd, lcd, lrp, imzml.
VENDOR_RAW_SUFFIXES: tuple[str, ...] = (
    ".raw", ".d", ".wiff", ".wiff2", ".lcd", ".qgd", ".abf", ".ibf", ".cdf", ".lrp",
)
CONVERTED_SUFFIXES: tuple[str, ...] = (".mzml", ".imzml")

# MS-DIAL HAS NO READER FOR THESE. SupportFormat.cs has no mzxml member, and the analyst who wrote
# the readers confirmed it on 2026-09-21, so listing one as an input would queue a run that cannot
# start. mzXML is the one the campaign converts: on 2026-09-30 the user decided that mzXML-only
# data are converted to mzML, so it is marked convertible, and which converter does it is the
# execution layer's choice rather than the Catalog's. The rest have no planned route to mzML.
UNREADABLE_SUFFIXES: tuple[str, ...] = (".mzxml", ".mzdata", ".mgf", ".ibd", ".dat", ".scan")
CONVERTIBLE_SUFFIXES: tuple[str, ...] = (".mzxml",)

# ONE ANALYSIS INPUT PER VENDOR CONTAINER. A Waters .raw and an Agilent or Bruker .d are folders,
# and a repository that enumerates its files lists every file inside them: MetaboBank MTBKS217
# positive names twelve folders in its SDRF and lists 477 files beneath them. Each of those files
# used to become an analysis input and a sample of its own, with no attributes -- 477 samples for
# twelve injections, the twelve real rows and their Factor Values dropped -- and every
# _FUNC*.DAT was marked for conversion, because ".dat" on its own is a format MS-DIAL cannot read.
# The folder is what MS-DIAL opens, so the folder is the input, as the user decided on
# 2026-09-30: one folder, one sample row, one analysis_files.csv row. The files inside it stay in
# the manifest with role vendor_folder_member and their container, because they are what is
# downloaded and checksummed; they are never inputs or samples.
FOLDER_CONTAINER_SUFFIXES: tuple[str, ...] = (".raw", ".d")
VENDOR_FOLDER_MEMBER_ROLE = "vendor_folder_member"
# A sample row may name a directory that is no MS-DIAL container at all: MetaboBank MTBKS225 names
# Bruker NMR experiment folders such as raw/0h_rep1/. The directory is one sample, and its files are
# its members, but it is never an MS-DIAL input.
DIRECTORY_MEMBER_ROLE = "directory_member"
MEMBER_ROLES = frozenset({VENDOR_FOLDER_MEMBER_ROLE, DIRECTORY_MEMBER_ROLE})

# A per-sample archive of one container -- x.d.zip, x.raw.rar, x.mzML.gz -- is that container
# packed: one sample and one input, the container it unpacks to. MetaboLights publishes 37,000 of
# them. Longest first, so that x.d.tar.gz is a tar.gz of x.d and not a gzip of x.d.tar. MTBLS688
# packs the 2,263 derived mzXML files of each of its two LC-MS units as x.mzXML.lzma.
PACKED_ARCHIVE_SUFFIXES: tuple[str, ...] = (
    ".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".zip", ".rar", ".7z", ".tar", ".lzma", ".gz",
    ".bz2", ".xz",
)

# WHAT TRAVELS WITH A SCIEX ACQUISITION. A .wiff.scan holds the spectra of its .wiff, and a
# .timeseries.data travels with a .wiff2; neither is opened on its own, so neither is an input,
# competes, or is marked for conversion, although ".scan" alone is a format MS-DIAL cannot read.
# .wiff2.scan is a sidecar as Interactive's _is_sidecar_name reads it. Packed, they are the same
# files: x.wiff.scan.zip was an input of its own, flagged for conversion, and Interactive excluded
# the whole SCIEX unit for it.
SIDECAR_SUFFIXES: tuple[str, ...] = (".wiff2.scan", ".wiff.scan")
AUXILIARY_SUFFIXES: tuple[str, ...] = (".timeseries.data",)

ANALYSIS_INPUT_MODEL = "one-input-per-sample.v1"

# WHAT STOPS A UNIT, AND WHAT ONLY WARNS. A folder two sample rows name, a sample row whose folder
# is not listed, a listed folder that no row names, and a declared directory that is not an MS-DIAL
# container each mean the sample table and the file listing disagree about what was measured, and a
# run would have to guess which row is which injection. Mixed containers do not: MTBKS219 mixes
# Bruker BAF (timsOFF) and TDF (timsON) folders, two ion-mobility regimes in one unit, and MTBKS222
# lists Waters folders beside SCIEX .wiff files; the remedy is to split the unit rather than to
# exclude it. Each travels as a warning with a split hint, because a blocker would take four of the
# eight in-scope folder units out of the campaign. A row naming the directory other rows' inputs
# sit in (raw/) names no sample, and is dropped with a warning, as it always was.
BLOCKING_INPUT_ISSUES: tuple[str, ...] = (
    "container_shared_by_samples",
    "sample_without_container",
    "container_without_sample",
    "declared_directory_not_msdial_input",
)
WARNING_INPUT_ISSUES: tuple[str, ...] = (
    "mixed_container_formats",
    "mixed_container_suffixes",
    "parent_directory_row",
)
# The format of a vendor folder whose member names do not say which it is.
UNKNOWN_FORMAT = "unknown"

# The inputs that are directories the listing enumerates. A per-sample archive is one published
# file like any other, and is attributed like one: a second row naming it is not a second injection,
# which is how the Catalog has always read a file two rows name.
_CONTAINER_KINDS = frozenset({"vendor_folder", "declared_directory"})
# Roles a file inside a vendor folder may arrive with. A converted file stands on its own, and an
# archive is unpacked rather than opened, so neither is ever a member.
_MEMBER_CANDIDATE_ROLES = frozenset({"raw", "sidecar", "auxiliary", "raw_alternate", *MEMBER_ROLES})
_ALL_CONTAINER_SUFFIXES: tuple[str, ...] = tuple(
    sorted(VENDOR_RAW_SUFFIXES + CONVERTED_SUFFIXES + UNREADABLE_SUFFIXES, key=len, reverse=True)
)
_TRAVELLING_SUFFIXES: tuple[str, ...] = SIDECAR_SUFFIXES + AUXILIARY_SUFFIXES
_ANALYSABLE_SUFFIXES: tuple[str, ...] = (
    VENDOR_RAW_SUFFIXES + CONVERTED_SUFFIXES + CONVERTIBLE_SUFFIXES
)
# Every container suffix is a single extension, so _container_suffix can look the last one up.
_SUFFIX_OF_EXTENSION: dict[str, str] = {suffix[1:]: suffix for suffix in _ALL_CONTAINER_SUFFIXES}
_WATERS_FUNCTION = re.compile(r"_func\d+\.dat")


def container_of(path: str) -> str:
    """The vendor folder a listed file lies inside, or "" when it lies inside none.

    The outermost segment ending in .raw or .d, other than the last: raw/x.raw/_FUNC001.DAT gives
    raw/x.raw, and a Bruker folder's marker file raw/x.d/x.d and its method raw/x.d/5512.m/lock.file
    both give raw/x.d. A Thermo x.raw is a file and gives "". A segment named just "raw" is a
    directory, not a container.
    """
    segments = _slashes(path).lstrip("/").split("/")
    for index, segment in enumerate(segments[:-1]):
        if _is_folder_container(segment):
            return "/".join(segments[: index + 1])
    return ""


def archived_container_of(path: str) -> str:
    """The container a per-sample archive unpacks to: x.d.zip gives x.d; "" when it packs none."""
    value = _slashes(path).lstrip("/")
    lower = value.casefold()
    if not lower.endswith(PACKED_ARCHIVE_SUFFIXES):
        return ""
    for archive in PACKED_ARCHIVE_SUFFIXES:
        if lower.endswith(archive):
            inner = value[: -len(archive)]
            suffix = _container_suffix(inner)
            name = inner.rsplit("/", 1)[-1]
            return inner if suffix and len(name) > len(suffix) else ""
    return ""


def unpacked_name(path: str) -> str:
    """The file a packed file unpacks to, when its name says what it packs; "" otherwise.

    A container, as archived_container_of reads it, or a file that travels with one: x.d.zip gives
    x.d, x.wiff.scan.zip gives x.wiff.scan and x.timeseries.data.gz gives x.timeseries.data. An
    accession archive such as ST000001_Rawdata.zip names nothing and gives "".
    """
    value = _slashes(path).lstrip("/")
    lower = value.casefold()
    if not lower.endswith(PACKED_ARCHIVE_SUFFIXES):
        return ""
    for archive in PACKED_ARCHIVE_SUFFIXES:
        if lower.endswith(archive):
            inner = value[: -len(archive)]
            name = inner.rsplit("/", 1)[-1].casefold()
            known = next((suffix for suffix in _TRAVELLING_SUFFIXES if name.endswith(suffix)), "")
            known = known or _container_suffix(name)
            return inner if known and len(name) > len(known) else ""
    return ""


def container_format(member_names: list[str]) -> str:
    """The vendor format of a folder, read from its members' names relative to the folder.

    Waters writes _FUNCnnn.DAT, Agilent an AcqData directory, and Bruker analysis.tdf, .tsf or
    .baf. The format is read from names because the Catalog has nothing else before a download;
    the execution layer re-derives it from the folder on disk, and the two must agree. "" when no
    name says, and the formats joined with "+" when names say more than one. The folder's analysis
    input then carries UNKNOWN_FORMAT, the token the split hint counts it under.
    """
    found: set[str] = set()
    for name in member_names:
        value = _slashes(name).lstrip("/").casefold()
        if _WATERS_FUNCTION.fullmatch(value):
            found.add("waters_raw")
        elif value.startswith("acqdata/"):
            found.add("agilent_d")
        elif value in {"analysis.tdf", "analysis.tsf", "analysis.baf"}:
            found.add("bruker_" + value.rsplit(".", 1)[-1])
    return "+".join(sorted(found))


def _container_stem(path: str) -> str:
    """The sample a container belongs to: its basename with the container suffix removed.

    _primary_stem strips only the .wiff family, because that is all it was written for. Pairing a
    vendor container with its converted twin needs the suffix gone whichever family it is from, so
    that 01026_Bread_nega.RAW and 01026_Bread_nega.mzXML name one sample rather than two. A packed
    container is read as the container it unpacks to, so x.d.zip pairs with x.mzML as x.d would.
    """
    value = _slashes(archived_container_of(path) or path).casefold().rsplit("/", 1)[-1]
    suffix = _container_suffix(value)
    return value[: -len(suffix)] if suffix else value


def _container_kind(path: str) -> str:
    value = _slashes(archived_container_of(path) or path).casefold()
    if value.endswith(UNREADABLE_SUFFIXES):
        return "unreadable"
    if value.endswith(CONVERTED_SUFFIXES):
        return "converted"
    if value.endswith(VENDOR_RAW_SUFFIXES):
        return "vendor"
    return ""


def normalize_file_roles(
    files: list[dict[str, Any]], samples: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Classify primary, alternate, sidecar, auxiliary and vendor-folder member files.

    `samples` resolves each folder member to the sample row that names its folder, and finds the
    declared directories; without it a member keeps the sample its folder already carried. A
    packed file that is no folder member says what it `unpacks_to`, and is classified as that
    file: x.wiff.scan.zip is a sidecar. `container` is the analysis input a file belongs to: the
    folder a member lies in, or the container a packed input unpacks to.
    """
    return _project_analysis_inputs({"files": files, "samples": samples or []})["files"]


def _project_analysis_inputs(unit: dict[str, Any]) -> dict[str, Any]:
    """Project a unit's stored rows onto its analysis inputs, in time linear in its files.

    LINEAR, BECAUSE THE UNITS ARE LARGE. This used to compare every primary file with every sample
    row and with every other file, which is quadratic: a scan of MetaboBank MTBKS263 (27,198 files)
    did not finish in ten minutes. Every comparison below is a dictionary lookup instead.

    Every Catalog view of a unit comes through here, and the view get_unit returns is projected again
    by Class selection and by the handoff, so the projection of a projection must be the projection.
    That is why an unmatched row that names a folder is kept rather than dropped, and why a folder
    with no row gets no invented row: either would make the second projection disagree with the first.
    A parent-directory row is dropped, so the view keeps it in `excluded_samples` and it is read
    back here, to be dropped again with the same warning.
    """
    rows = [
        dict(item)
        for item in [*(unit.get("samples", []) or []), *(unit.get("excluded_samples", []) or [])]
    ]
    files = [dict(item) for item in unit.get("files", []) or []]
    known_paths: set[str] = set()
    for item in files:
        path = _slashes(_file_path(item)).lstrip("/").casefold()
        known_paths.update(filter(None, (path, unpacked_name(path))))
    for item in files:
        item["role"] = _file_role(item, known_paths)

    declared = _declared_directories(rows, files)
    named = _named_paths(rows, declared)
    parents = _parent_directories(files, declared, named)
    excluded = [row for row in rows if _directory_row_key(row) in parents]
    if excluded:
        rows = [row for row in rows if _directory_row_key(row) not in parents]
        declared = {key: path for key, path in declared.items() if key not in parents}
    groups = _group_members(files, declared, named)
    _prefer_one_container_per_sample(
        files, [group["path"] for group in groups.values() if group["kind"] == "vendor_folder"]
    )

    inputs: list[dict[str, Any]] = []
    members: dict[int, list[dict[str, Any]]] = {}
    sources: dict[int, str] = {}
    seen: set[str] = set()
    for item in files:
        role = item["role"]
        if role in MEMBER_ROLES:
            key = item["container"].casefold()
            if key in seen:
                continue
            seen.add(key)
            group = groups[key]
            entry = _analysis_input(group["path"], group["kind"], group["members"], group["format"])
            source, listed = group["path"], group["members"]
        else:
            unpacked = unpacked_name(_file_path(item))
            if unpacked:
                item["unpacks_to"] = unpacked
            if role != "raw":
                continue
            source, listed = _file_path(item), [item]
            inner = archived_container_of(source)
            if inner:
                item["container"] = inner
                entry = _analysis_input(inner, "archived_container", listed)
                entry["archive"] = source
            else:
                entry = _analysis_input(source, "file", listed)
        inputs.append(entry)
        members[id(entry)] = listed
        sources[id(entry)] = source

    samples, issues = _attribute_samples(rows, inputs, members, sources, files)
    _resolve_file_samples(files, inputs, members, keep_declared=not rows)
    issues.extend(_format_issues(inputs))
    if excluded:
        issues.append(
            {
                "code": "parent_directory_row",
                "blocking": False,
                "count": len(excluded),
                "examples": [str(row.get("raw_file") or "") for row in excluded][:5],
                "message": (
                    "sample rows name a directory that holds other rows' inputs, so they name no "
                    "sample of their own and were dropped"
                ),
            }
        )
    return {
        "files": files,
        "inputs": inputs,
        "samples": samples,
        "excluded": excluded,
        "issues": issues,
        "split_hint": _split_hint(issues),
    }


def _directory_row_key(row: dict[str, Any]) -> str:
    """The directory a sample row names with a trailing "/", matched as declared directories are."""
    raw = _slashes(row.get("raw_file")).strip()
    return _match_key(raw) if raw.endswith("/") else ""


def _named_paths(
    rows: list[dict[str, Any]], declared: dict[str, str]
) -> tuple[set[str], set[str]]:
    """The paths the sample rows name, and their basenames, leaving out the declared directories."""
    exact: set[str] = set()
    for row in rows:
        key = _match_key(row.get("raw_file"))
        if key and key not in declared:
            exact.add(key)
    return exact, {key.rsplit("/", 1)[-1] for key in exact}


def _is_named(path: str, named: tuple[set[str], set[str]]) -> bool:
    """Whether a sample row names this file or folder, by its path or by its basename."""
    exact, names = named
    if not exact:
        return False
    for value in filter(None, (path, unpacked_name(path))):
        key = _match_key(value)
        if key in exact or key.rsplit("/", 1)[-1] in names:
            return True
    return False


def _is_analysable(path: str) -> bool:
    """Whether MS-DIAL opens a file, or the campaign converts it (mzXML), packed or not."""
    return _slashes(archived_container_of(path) or path).casefold().endswith(_ANALYSABLE_SUFFIXES)


def _parent_directories(
    files: list[dict[str, Any]], declared: dict[str, str], named: tuple[set[str], set[str]]
) -> set[str]:
    """The declared directories that hold another row's input, keyed case-insensitively.

    A PARENT DIRECTORY IS NO SAMPLE. A row whose raw_file is raw/, beside rows naming raw/a.mzML
    and raw/b.mzML, names the directory the samples sit in. Read as a sample directory it absorbed
    every file below it, the other rows' own files included, and blocked the unit as a directory
    MS-DIAL cannot open; before the unit view grouped folders, it matched no file and was dropped.
    It is dropped again, with a warning. A directory holds another row's input when a file or
    vendor folder below it is one a row names, or when a deeper declared directory takes its
    files. A container no row names does not count: it is no row's input, and a sample directory
    holding one stray file must not be dropped for it and leave its other files as inputs.
    """
    if not declared:
        return set()
    nested = any(len(_declared_ancestors(key + "/", declared)) > 1 for key in declared)
    if not named[0] and not nested:
        # No row names a file or folder, and no declared directory lies in another (MTBKS263's
        # 1,236 NMR experiments): none of them can hold another row's input.
        return set()
    parents: set[str] = set()
    for item in files:
        if item["role"] not in _MEMBER_CANDIDATE_ROLES:
            continue
        path = _slashes(_file_path(item)).lstrip("/")
        above = _declared_ancestors(path, declared)
        if not above:
            continue
        folder = container_of(path)
        if _is_named(folder or path, named):
            parents.update(above)
        elif not folder and not _is_analysable(path):
            # The innermost declared directory takes this file; every one above it holds that one.
            parents.update(above[:-1])
    return parents


def _declared_directories(rows: list[dict[str, Any]], files: list[dict[str, Any]]) -> dict[str, str]:
    """Directories sample rows name that are not vendor containers, keyed case-insensitively."""
    declared: dict[str, str] = {}
    for row in rows:
        raw = _slashes(row.get("raw_file")).lstrip("/")
        path = raw.rstrip("/")
        # The trailing "/" is kept for container_of, so that the last segment counts as well.
        if raw.endswith("/") and path and not container_of(path + "/"):
            declared.setdefault(path.casefold(), path)
    for item in files:
        # The rows may no longer say so once projected; the members still do.
        if item.get("role") == DIRECTORY_MEMBER_ROLE and item.get("container"):
            declared.setdefault(str(item["container"]).casefold(), str(item["container"]))
    return declared


def _group_members(
    files: list[dict[str, Any]], declared: dict[str, str], named: tuple[set[str], set[str]]
) -> dict[str, dict[str, Any]]:
    """Mark every file inside a vendor folder or a declared directory as a member of it.

    A declared directory takes only what nothing else claims: never a container MS-DIAL opens or an
    mzXML the campaign converts, and never a file a sample row names, because each of those is an
    input of its own. Of two nested declared directories the inner one, the more specific, takes it.
    """
    groups: dict[str, dict[str, Any]] = {}
    for item in files:
        if item["role"] not in _MEMBER_CANDIDATE_ROLES:
            continue
        path = _slashes(_file_path(item)).lstrip("/")
        container, kind = container_of(path), "vendor_folder"
        if not container and declared:
            directory = _declared_directory_of(path, declared)
            if directory and not _is_analysable(path) and not _is_named(path, named):
                container, kind = directory, "declared_directory"
        if not container:
            if item["role"] in MEMBER_ROLES:
                # Said to be a member of a container its path does not lie in: read it as a file.
                item["role"] = "raw"
                item.pop("container", None)
            continue
        group = groups.setdefault(
            container.casefold(), {"path": container, "kind": kind, "members": []}
        )
        group["members"].append(item)
        item["role"] = VENDOR_FOLDER_MEMBER_ROLE if kind == "vendor_folder" else DIRECTORY_MEMBER_ROLE
        item["container"] = group["path"]
        # A member is never an input, so nothing about it can be a reason to demote or convert it.
        item.pop("requires_conversion", None)
        item.pop("conversion_target", None)
        item.pop("demoted_because", None)
    for group in groups.values():
        offset = len(group["path"]) + 1
        group["format"] = ""
        if group["kind"] == "vendor_folder":
            group["format"] = container_format(
                [_slashes(_file_path(item)).lstrip("/")[offset:] for item in group["members"]]
            ) or UNKNOWN_FORMAT
    return groups


def _declared_directory_of(path: str, declared: dict[str, str]) -> str:
    """The innermost declared directory a file lies in, or ""."""
    above = _declared_ancestors(path, declared)
    return declared[above[-1]] if above else ""


def _declared_ancestors(path: str, declared: dict[str, str]) -> list[str]:
    """The keys of the declared directories a file lies in, outermost first."""
    if not declared:
        return []
    found: list[str] = []
    prefix = ""
    for segment in path.split("/")[:-1]:
        prefix = f"{prefix}/{segment}" if prefix else segment
        key = prefix.casefold()
        if key in declared:
            found.append(key)
    return found


def _analysis_input(
    path: str, kind: str, members: list[dict[str, Any]], vendor_format: str = ""
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "path": path,
        "kind": kind,
        "suffix": "" if kind == "declared_directory" else _container_suffix(path),
        "format": vendor_format,
        "member_count": len(members),
        "size_bytes": sum(int(item.get("size_bytes") or 0) for item in members),
        "sample_id": "",
    }
    if kind in {"file", "archived_container"}:
        # The input says what has to happen to it, as its file does: an input read on its own must
        # not look like a format with no conversion planned.
        entry["requires_conversion"] = bool(members[0].get("requires_conversion"))
        entry["conversion_target"] = str(members[0].get("conversion_target") or "")
    return entry


def _attribute_samples(
    rows: list[dict[str, Any]],
    inputs: list[dict[str, Any]],
    members: dict[int, list[dict[str, Any]]],
    sources: dict[int, str],
    files: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Give each analysis input the sample row that names it, and say where the two disagree."""
    if not inputs:
        return rows, []
    exact: dict[str, list[int]] = defaultdict(list)
    by_name: dict[str, list[int]] = defaultdict(list)
    by_id: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        key = _match_key(row.get("raw_file"))
        if key:
            exact[key].append(index)
            by_name[key.rsplit("/", 1)[-1]].append(index)
        sample_id = str(row.get("sample_id") or "").casefold()
        if sample_id:
            by_id[sample_id].append(index)
    related: dict[str, list[str]] = defaultdict(list)
    for item in files:
        if item["role"] != "raw" and item["role"] not in MEMBER_ROLES:
            related[_primary_stem(_file_path(item))].append(_file_path(item))

    # EVERY EXACT PATH FIRST, THEN THE FALLBACKS. One pass in input order let an input's fallback
    # take a row that a later input names exactly: FILES/raw/x.mzML sorts before raw/x.mzML and
    # took the row naming the second, by the leading directory the two differ by. Every input first
    # takes the unused rows naming it exactly; only the inputs left without one then try a name,
    # and then the sample id their files carry.
    used: set[int] = set()
    keys_of: list[list[str]] = []
    claims: dict[int, list[int]] = {}
    for position, entry in enumerate(inputs):
        keys = [_match_key(entry["path"])]
        if entry.get("archive"):
            keys.append(_match_key(entry["archive"]))
        keys_of.append(keys)
        claimed = [index for key in keys for index in exact.get(key, ()) if index not in used]
        claimed = list(dict.fromkeys(claimed))
        if entry["kind"] not in _CONTAINER_KINDS:
            # One file, one row: a second row naming the same file is not a second injection.
            claimed = claimed[:1]
        if claimed:
            used.update(claimed)
            claims[position] = claimed
    for position, entry in enumerate(inputs):
        if position in claims:
            continue
        claimed = _claim_by_name(keys_of[position], rows, by_name, used)
        if not claimed:
            declared_id = _declared_sample_id(members[id(entry)])
            candidates = [index for index in by_id.get(declared_id.casefold(), ()) if index not in used]
            claimed = candidates[:1] if declared_id else []
        if claimed:
            used.update(claimed)
            claims[position] = claimed

    samples: list[dict[str, Any]] = []
    shared: list[str] = []
    unnamed: list[str] = []
    declared: list[str] = []
    for position, entry in enumerate(inputs):
        container = entry["kind"] in _CONTAINER_KINDS
        claimed = claims.get(position, [])
        source = sources[id(entry)]
        if entry["kind"] == "declared_directory":
            declared.append(entry["path"])
        if not claimed:
            if container:
                # No row names this container, and inventing one would give it a sample id that the
                # repository never used and a projection of this view would then take as real.
                unnamed.append(entry["path"])
                continue
            entry["sample_id"] = _declared_sample_id(members[id(entry)]) or _primary_stem(source)
            samples.append(
                {
                    "sample_id": entry["sample_id"],
                    "raw_file": source,
                    "attributes": {},
                    "contexts": [],
                    "related_files": list(related.get(_primary_stem(source), ())),
                }
            )
            continue
        if len(claimed) > 1:
            shared.append(entry["path"])
            entry["sample_ids"] = [str(rows[index].get("sample_id") or "") for index in claimed]
        else:
            entry["sample_id"] = str(rows[claimed[0]].get("sample_id") or "")
        for index in claimed:
            sample = dict(rows[index])
            if entry["kind"] != "declared_directory":
                # The folder itself, with no trailing slash: raw/x.raw/ names the same folder as
                # raw/x.raw, and only the second has a basename.
                sample["raw_file"] = source
            sample["related_files"] = list(related.get(_primary_stem(source), ()))
            samples.append(sample)

    has_folders = any(entry["kind"] == "vendor_folder" for entry in inputs)
    missing: list[str] = []
    for index, row in enumerate(rows):
        if index in used:
            continue
        raw = _slashes(row.get("raw_file"))
        name = raw.rstrip("/").rsplit("/", 1)[-1]
        if raw.endswith("/") or (has_folders and _is_folder_container(name)):
            # A row that names a container the listing does not hold is a sample nobody can analyse.
            # It is kept, so that it is seen and so that a projection of this view still finds it.
            missing.append(str(row.get("raw_file") or ""))
            samples.append({**row, "related_files": []})

    issues: list[dict[str, Any]] = []
    for code, examples, message in (
        (
            "container_shared_by_samples",
            shared,
            "several sample rows name one container, so which row is the injection is not stated",
        ),
        (
            "sample_without_container",
            missing,
            "sample rows name a container that the file listing does not hold",
        ),
        (
            "container_without_sample",
            unnamed,
            "the file listing holds a container that no sample row names",
        ),
        (
            "declared_directory_not_msdial_input",
            declared,
            "sample rows name directories that are not MS-DIAL containers, such as Bruker NMR "
            "experiment folders",
        ),
    ):
        if examples:
            issues.append(
                {
                    "code": code,
                    "blocking": True,
                    "count": len(examples),
                    "examples": examples[:5],
                    "message": message,
                }
            )
    return samples, issues


def _claim_by_name(
    keys: list[str], rows: list[dict[str, Any]], by_name: dict[str, list[int]], used: set[int]
) -> list[int]:
    """A row whose raw_file differs from the listed path only by leading directories.

    FILES/a/x.mzML and a/x.mzML name one file; so do a/x.mzML and x.mzML when x.mzML is the only
    row of that name. A basename two rows share names neither of them, because taking the first
    would pair a sample with another sample's file.
    """
    for key in keys:
        name = key.rsplit("/", 1)[-1]
        candidates = [index for index in by_name.get(name, ()) if index not in used]
        nested = [
            index
            for index in candidates
            if _nested_path(_match_key(rows[index].get("raw_file")), key)
        ]
        if len(nested) == 1:
            return nested
        if len(by_name.get(name, ())) == 1 and candidates:
            return candidates
    return []


def _nested_path(left: str, right: str) -> bool:
    return left == right or left.endswith("/" + right) or right.endswith("/" + left)


def _declared_sample_id(members: list[dict[str, Any]]) -> str:
    """The sample id the listed files already carry, when they all carry the same one."""
    values = {str(item.get("sample_id") or "") for item in members} - {""}
    return values.pop() if len(values) == 1 else ""


def _resolve_file_samples(
    files: list[dict[str, Any]],
    inputs: list[dict[str, Any]],
    members: dict[int, list[dict[str, Any]]],
    *,
    keep_declared: bool = False,
) -> None:
    """Point every file at the analytical sample it belongs to, or say plainly that it belongs to none.

    A sidecar or auxiliary container arrives carrying a sample_id derived from its own basename,
    which names no row in the sample table: half a manifest's rows held a dangling key, and anything
    joining files to samples on it either dropped those rows or re-invented the samples. A folder
    member takes the sample row that names its folder; with no sample table to read
    (`keep_declared`), it keeps the one its folder already carried.
    """
    for entry in inputs:
        if entry["kind"] not in {"vendor_folder", "declared_directory"}:
            continue
        sample_id = entry["sample_id"]
        if keep_declared and not sample_id:
            sample_id = _declared_sample_id(members[id(entry)])
        for item in members[id(entry)]:
            item["sample_id"] = sample_id
            item["sample_id_resolved"] = bool(sample_id)
    primary_by_stem: dict[str, dict[str, Any]] = {}
    for item in files:
        if item["role"] == "raw":
            primary_by_stem.setdefault(_primary_stem(_file_path(item)), item)
    for item in files:
        if item["role"] in MEMBER_ROLES:
            continue
        if item["role"] == "raw":
            item["sample_id_resolved"] = True
            continue
        parent = primary_by_stem.get(_primary_stem(_file_path(item)))
        if parent is None:
            item["sample_id"] = ""
            item["sample_id_resolved"] = False
            continue
        item["sample_id"] = str(parent.get("sample_id") or "")
        item["parent_file"] = _file_path(parent)
        item["sample_id_resolved"] = bool(item["sample_id"])


def _format_issues(inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Warn when a unit's vendor containers need more than one run, with the count of each kind.

    Vendor folders of more than one format are one case. Vendor folders beside vendor files of
    another suffix are the other: MTBKS222 lists nine Waters .raw folders beside twelve SCIEX .wiff
    files in one unit, and the formats alone could not say so, because a file carries none.
    """
    issues: list[dict[str, Any]] = []
    formats: Counter[str] = Counter(
        entry["format"] for entry in inputs if entry["kind"] == "vendor_folder"
    )
    if len(set(formats) - {UNKNOWN_FORMAT}) >= 2:
        issues.append(
            {
                "code": "mixed_container_formats",
                "blocking": False,
                "count": sum(formats.values()),
                "formats": dict(sorted(formats.items())),
                "message": (
                    "the unit's vendor folders are of more than one format ("
                    + ", ".join(f"{name} {count}" for name, count in sorted(formats.items()))
                    + "); run them as separate parts, one format each"
                ),
            }
        )
    folder_suffixes = {entry["suffix"] for entry in inputs if entry["kind"] == "vendor_folder"}
    file_suffixes = {
        entry["suffix"]
        for entry in inputs
        if entry["kind"] != "vendor_folder" and entry["suffix"] in VENDOR_RAW_SUFFIXES
    }
    if folder_suffixes and file_suffixes - folder_suffixes:
        suffixes: Counter[str] = Counter(
            entry["suffix"] for entry in inputs if entry["suffix"] in VENDOR_RAW_SUFFIXES
        )
        issues.append(
            {
                "code": "mixed_container_suffixes",
                "blocking": False,
                "count": sum(suffixes.values()),
                "suffixes": dict(sorted(suffixes.items())),
                "message": (
                    "the unit's vendor folders sit beside vendor files of another suffix ("
                    + ", ".join(f"{name} {count}" for name, count in sorted(suffixes.items()))
                    + "); run them as separate parts, one suffix each"
                ),
            }
        )
    return issues


def _split_hint(issues: list[dict[str, Any]]) -> dict[str, Any] | None:
    """How to split the unit so that each MS-DIAL run holds one kind of container, or None.

    `key` names the analysis_inputs field whose value is an input's group, and `groups` counts the
    inputs of each value, with the tokens analysis_inputs documents. A unit with mixed suffixes is
    split by suffix first; when its folders are of more than one format as well, the part holding
    them still carries the mixed_container_formats warning and is split again by format.
    """
    by_suffix = next((item for item in issues if item["code"] == "mixed_container_suffixes"), None)
    if by_suffix is not None:
        return {
            "key": "suffix",
            "groups": dict(by_suffix["suffixes"]),
            "reason": (
                "One MS-DIAL run reads one vendor's containers: Waters .raw folders and SCIEX "
                ".wiff files are two instruments. Each analysis_inputs entry carries its suffix."
            ),
        }
    by_format = next((item for item in issues if item["code"] == "mixed_container_formats"), None)
    if by_format is None:
        return None
    return {
        "key": "format",
        "groups": dict(by_format["formats"]),
        "reason": (
            "One MS-DIAL run holds one container format: Bruker BAF and TDF folders are two "
            "ion-mobility regimes. Each analysis_inputs entry carries its format, and a folder "
            f"whose member names do not say is {UNKNOWN_FORMAT!r} in both places."
        ),
    }


def _prefer_one_container_per_sample(
    files: list[dict[str, Any]], folders: list[str] | None = None
) -> None:
    """Leave exactly one analysable container per sample, demoting the rest to raw_alternate.

    WHAT THIS ENDS. MetaboBank MTBKS157 publishes each of its sixteen samples twice, once as .RAW
    and once as .mzXML, and both arrived with role "raw" -- thirty-two analysis inputs for sixteen
    samples. MS-DIAL would have detected every peak twice and aligned each sample against its own
    second encoding, which looks like perfect reproducibility and is an artefact of the manifest.

    The vendor container wins. Its readers are the more stable of the two in practice, which is the
    analyst's instruction of 2026-09-21; the demoted file keeps role raw_alternate rather than
    being dropped, so a run that cannot read the vendor format can still find it. A vendor folder
    (`folders`, whose members are listed rather than the folder) competes as the vendor container
    it is, so an x.mzML published beside an x.raw/ folder is the alternate.

    The .wiff/.wiff2 pair is decided separately and earlier, in _file_role: .wiff2 is demoted when
    a .wiff for the same sample exists. That default is right for every acquisition except SCIEX
    ZT Scan DIA, where the .wiff2 is the one to read -- and nothing in repository metadata
    establishes that an acquisition was ZT Scan DIA, so it is not guessed at here. Resolving it
    needs the .wiff2 header, which only the raw-header preflight can read.

    ONE CONTAINER, LISTED TWICE. A vendor folder enumerated member by member beside its own
    archive (raw/x.raw/... and raw/x.raw.zip), or a file beside its own archive (x.mzML and
    x.mzML.gz), is one container at one path, and gave two inputs of that path. The two are of
    equal preference by kind, so this is decided first: the one listed unpacked is analysed, and
    the archive is the download alternative.
    """
    by_stem: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in folders or []:
        # Stands in for the folder; it is never demoted, because it is always a vendor container.
        by_stem[_container_stem(path)].append({"path": path, "role": "raw"})
    for item in files:
        if item.get("role") != "raw":
            continue
        path = _file_path(item)
        if _container_kind(path):
            by_stem[_container_stem(path)].append(item)
    for _stem, group in by_stem.items():
        unpacked = {
            _slashes(_file_path(item)).casefold()
            for item in group
            if not archived_container_of(_file_path(item))
        }
        for item in group:
            inner = archived_container_of(_file_path(item))
            if inner and _slashes(inner).casefold() in unpacked:
                item["role"] = "raw_alternate"
                item["demoted_because"] = "the container it packs is listed unpacked beside it"
        kinds = {id(item): _container_kind(_file_path(item)) for item in group}
        # A format MS-DIAL cannot open is never the file to analyse. It is marked wherever it
        # appears, alone or not, so a unit holding only these says so rather than queueing a run
        # that cannot start.
        for item in group:
            if kinds[id(item)] == "unreadable":
                path = archived_container_of(_file_path(item)) or _file_path(item)
                if path.casefold().endswith(CONVERTIBLE_SUFFIXES):
                    item["requires_conversion"] = (
                        "MS-DIAL has no reader for this format; it must be converted to mzML "
                        "before it is analysed"
                    )
                    item["conversion_target"] = "mzML"
                else:
                    item["requires_conversion"] = (
                        "MS-DIAL has no reader for this format, and no conversion to one it reads "
                        "is planned"
                    )
        competing = [item for item in group if item["role"] == "raw"]
        if len(competing) < 2:
            continue
        readable = {id(item) for item in competing if kinds[id(item)] in {"vendor", "converted"}}
        vendor = {id(item) for item in competing if kinds[id(item)] == "vendor"}
        preferred = vendor or readable
        if not preferred or len(preferred) == len(competing):
            # Either nothing MS-DIAL can read, or the duplicates are equally preferred and this
            # rule has no opinion about which of them to open.
            continue
        for item in competing:
            if id(item) not in preferred:
                item["role"] = "raw_alternate"
                item["demoted_because"] = (
                    "a vendor raw container for the same sample is published alongside it"
                    if vendor
                    else "MS-DIAL cannot read this format and a readable container is published "
                    "alongside it"
                )


def normalize_analysis_unit(unit: dict[str, Any]) -> dict[str, Any]:
    """Return the canonical analysis-unit view used by every Catalog surface."""
    projection = _project_analysis_inputs(unit)
    normalized = {**unit, "files": projection["files"]}
    normalized["samples"] = projection["samples"]
    normalized["sample_count"] = len(projection["samples"])
    normalized["analysis_inputs"] = projection["inputs"]
    normalized["analysis_file_count"] = len(projection["inputs"])
    normalized["analysis_input_issues"] = projection["issues"]
    normalized["split_hint"] = projection["split_hint"]
    normalized["excluded_samples"] = projection["excluded"]
    return normalized


def _file_role(item: dict[str, Any], known_paths: set[str]) -> str:
    # A packed file is read as the file it unpacks to, for every rule here: x.wiff.scan.zip is a
    # sidecar, packed, and x.wiff.zip the alternate of an x.wiff2.zip beside it.
    path = _slashes(_file_path(item)).lstrip("/").casefold()
    path = unpacked_name(path) or path
    if path.endswith(AUXILIARY_SUFFIXES):
        return "auxiliary"
    if path.endswith(SIDECAR_SUFFIXES):
        return "sidecar"
    role = str(item.get("role") or "raw")
    # WIFF2 WINS, ALWAYS. This used to demote the .wiff2 when a .wiff for the same sample was
    # published beside it. The two encode the same acquisition, and exactly one may be analysed or
    # the sample is measured twice; which one to keep was decided by the analyst on 2026-09-21 in
    # favour of .wiff2 for every acquisition, rather than reading .wiff2 only for SCIEX ZT Scan
    # DIA. The narrower rule would have needed the acquisition method, which no repository field
    # states and only the .wiff2 header carries -- so it would have been a guess wearing the
    # clothes of a decision, and the campaign has enough of those.
    if path.endswith(".wiff") and path.removesuffix(".wiff") + ".wiff2" in known_paths:
        return "raw_alternate"
    return role


def _primary_stem(path: str) -> str:
    """The name a SCIEX file shares with the files that travel with it, packed or not."""
    value = path.replace("\\", "/").casefold()
    value = unpacked_name(value) or value
    for suffix in (*AUXILIARY_SUFFIXES, *SIDECAR_SUFFIXES, ".wiff2", ".wiff"):
        if value.endswith(suffix):
            return value[: -len(suffix)]
    return value


def _file_path(item: dict[str, Any]) -> str:
    return str(item.get("path") or item.get("name") or "")


def _slashes(value: Any) -> str:
    return str(value or "").replace("\\", "/")


def _match_key(value: Any) -> str:
    return _slashes(value).strip().lstrip("/").rstrip("/").casefold()


def _container_suffix(path: str) -> str:
    _, dot, extension = _slashes(path).casefold().rpartition(".")
    return _SUFFIX_OF_EXTENSION.get(extension, "") if dot else ""


def _is_folder_container(segment: str) -> bool:
    value = segment.casefold()
    # A segment that is only the suffix (".d") names no folder.
    return value.endswith(FOLDER_CONTAINER_SUFFIXES) and value not in FOLDER_CONTAINER_SUFFIXES


def _field_priority(field: str) -> float:
    text = field.casefold()
    if any(
        token in text
        for token in (
            "analyticalcondition", "analytical condition", "instrument", "chromatograph",
            "ionization", "collision", "fragmentation", "mass range", "scan mode",
        )
    ):
        return -8.0
    priorities = (
        ("disease", 6), ("treatment", 6), ("genotype", 6), ("condition", 5),
        ("cell type", 5), ("tissue", 5), ("organ", 5), ("age", 5),
        ("sex", 4), ("diet", 4), ("time", 3), ("batch", 2),
        ("analytical order", 1), ("instrument", -3), ("file", -4),
    )
    return float(next((value for token, value in priorities if _field_contains(text, token)), 0))


def _field_contains(text: str, token: str) -> bool:
    escaped = re.escape(token).replace(r"\ ", r"\s+")
    return bool(re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", text))


def _class_token(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).strip().replace("_", "-")
    text = re.sub(r"\s+", "-", text)
    text = "".join(character if character.isalnum() or character in ".+-" else "-" for character in text)
    return re.sub(r"-+", "-", text).strip("-")


def _scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(filter(None, (_scalar(item) for item in value)))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return re.sub(r"\s+", " ", str(value)).strip()

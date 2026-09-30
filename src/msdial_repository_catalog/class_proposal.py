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
    well, and the unit's analysis_input_issues say so.
    """
    return _project_analysis_inputs(unit)["samples"]


def analysis_inputs(unit: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the unit's analysis inputs: one per file, vendor folder or packed container.

    Each entry names what MS-DIAL opens (`path`, relative to the download root), its `kind`, the
    container `suffix`, the vendor `format` read from a folder's member names, how many listed
    files make it up and their bytes, and the sample row that names it.
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
# them. Longest first, so that x.d.tar.gz is a tar.gz of x.d and not a gzip of x.d.tar.
PACKED_ARCHIVE_SUFFIXES: tuple[str, ...] = (
    ".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
)

ANALYSIS_INPUT_MODEL = "one-input-per-sample.v1"

# WHAT STOPS A UNIT, AND WHAT ONLY WARNS. A folder two sample rows name, a sample row whose folder
# is not listed, a listed folder that no row names, and a declared directory that is not an MS-DIAL
# container each mean the sample table and the file listing disagree about what was measured, and a
# run would have to guess which row is which injection. Mixed container formats do not: MTBKS219
# mixes Bruker BAF (timsOFF) and TDF (timsON) folders, two ion-mobility regimes in one unit, and the
# remedy is to split it by format rather than to exclude it. It travels as a warning with a split
# hint, because a blocker would take four of the eight in-scope folder units out of the campaign.
BLOCKING_INPUT_ISSUES: tuple[str, ...] = (
    "container_shared_by_samples",
    "sample_without_container",
    "container_without_sample",
    "declared_directory_not_msdial_input",
)
WARNING_INPUT_ISSUES: tuple[str, ...] = ("mixed_container_formats",)

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
    for archive in PACKED_ARCHIVE_SUFFIXES:
        if lower.endswith(archive):
            inner = value[: -len(archive)]
            suffix = _container_suffix(inner)
            name = inner.rsplit("/", 1)[-1]
            return inner if suffix and len(name) > len(suffix) else ""
    return ""


def container_format(member_names: list[str]) -> str:
    """The vendor format of a folder, read from its members' names relative to the folder.

    Waters writes _FUNCnnn.DAT, Agilent an AcqData directory, and Bruker analysis.tdf, .tsf or
    .baf. The format is read from names because the Catalog has nothing else before a download;
    the execution layer re-derives it from the folder on disk, and the two must agree. "" when no
    name says, and the formats joined with "+" when names say more than one.
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
    for suffix in UNREADABLE_SUFFIXES:
        if value.endswith(suffix):
            return "unreadable"
    for suffix in CONVERTED_SUFFIXES:
        if value.endswith(suffix):
            return "converted"
    for suffix in VENDOR_RAW_SUFFIXES:
        if value.endswith(suffix):
            return "vendor"
    return ""


def normalize_file_roles(
    files: list[dict[str, Any]], samples: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Classify primary, alternate, sidecar, auxiliary and vendor-folder member files.

    `samples` resolves each folder member to the sample row that names its folder, and finds the
    declared directories; without it a member keeps the sample its folder already carried.
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
    """
    rows = [dict(item) for item in unit.get("samples", []) or []]
    files = [dict(item) for item in unit.get("files", []) or []]
    known_paths = {_slashes(_file_path(item)).casefold() for item in files}
    for item in files:
        item["role"] = _file_role(item, known_paths)

    groups = _group_members(files, _declared_directories(rows, files))
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
        elif role == "raw":
            source, listed = _file_path(item), [item]
            inner = archived_container_of(source)
            if inner:
                item["container"] = inner
                entry = _analysis_input(inner, "archived_container", listed)
                entry["archive"] = source
            else:
                entry = _analysis_input(source, "file", listed)
        else:
            continue
        inputs.append(entry)
        members[id(entry)] = listed
        sources[id(entry)] = source

    samples, issues = _attribute_samples(rows, inputs, members, sources, files)
    _resolve_file_samples(files, inputs, members, keep_declared=not rows)
    issues.extend(_format_issues(inputs))
    return {
        "files": files,
        "inputs": inputs,
        "samples": samples,
        "issues": issues,
        "split_hint": _split_hint(issues),
    }


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
    files: list[dict[str, Any]], declared: dict[str, str]
) -> dict[str, dict[str, Any]]:
    """Mark every file inside a vendor folder or a declared directory as a member of it."""
    groups: dict[str, dict[str, Any]] = {}
    for item in files:
        if item["role"] not in _MEMBER_CANDIDATE_ROLES:
            continue
        path = _slashes(_file_path(item)).lstrip("/")
        container, kind = container_of(path), "vendor_folder"
        if not container:
            container, kind = _declared_directory_of(path, declared), "declared_directory"
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
        group["format"] = (
            container_format(
                [_slashes(_file_path(item)).lstrip("/")[offset:] for item in group["members"]]
            )
            if group["kind"] == "vendor_folder"
            else ""
        )
    return groups


def _declared_directory_of(path: str, declared: dict[str, str]) -> str:
    if not declared:
        return ""
    segments = path.split("/")
    prefix = ""
    for segment in segments[:-1]:
        prefix = f"{prefix}/{segment}" if prefix else segment
        hit = declared.get(prefix.casefold())
        if hit:
            return hit
    return ""


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
        entry["requires_conversion"] = bool(members[0].get("requires_conversion"))
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

    used: set[int] = set()
    samples: list[dict[str, Any]] = []
    shared: list[str] = []
    unnamed: list[str] = []
    declared: list[str] = []
    for entry in inputs:
        container = entry["kind"] in _CONTAINER_KINDS
        keys = [_match_key(entry["path"])]
        if entry.get("archive"):
            keys.append(_match_key(entry["archive"]))
        claimed = [index for key in keys for index in exact.get(key, ()) if index not in used]
        claimed = list(dict.fromkeys(claimed))
        if not container:
            # One file, one row: a second row naming the same file is not a second injection.
            claimed = claimed[:1]
        if not claimed:
            claimed = _claim_by_name(keys, rows, by_name, used)
        if not claimed:
            declared_id = _declared_sample_id(members[id(entry)])
            candidates = [index for index in by_id.get(declared_id.casefold(), ()) if index not in used]
            claimed = candidates[:1] if declared_id else []
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
        used.update(claimed)
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
    """Warn when a unit's vendor folders are of more than one format, with the count of each."""
    counts: Counter[str] = Counter(
        entry["format"] or "unknown" for entry in inputs if entry["kind"] == "vendor_folder"
    )
    if len(set(counts) - {"unknown"}) < 2:
        return []
    return [
        {
            "code": "mixed_container_formats",
            "blocking": False,
            "count": sum(counts.values()),
            "formats": dict(sorted(counts.items())),
            "message": (
                "the unit's vendor folders are of more than one format ("
                + ", ".join(f"{name} {count}" for name, count in sorted(counts.items()))
                + "); run them as separate parts, one format each"
            ),
        }
    ]


def _split_hint(issues: list[dict[str, Any]]) -> dict[str, Any] | None:
    mixed = next((item for item in issues if item["code"] == "mixed_container_formats"), None)
    if mixed is None:
        return None
    return {
        "key": "format",
        "groups": dict(mixed["formats"]),
        "reason": (
            "One MS-DIAL run holds one container format: Bruker BAF and TDF folders are two "
            "ion-mobility regimes. Each analysis_inputs entry carries its format."
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
        if len(group) < 2:
            continue
        readable = {id(item) for item in group if kinds[id(item)] in {"vendor", "converted"}}
        vendor = {id(item) for item in group if kinds[id(item)] == "vendor"}
        preferred = vendor or readable
        if not preferred or len(preferred) == len(group):
            # Either nothing MS-DIAL can read, or the duplicates are equally preferred and this
            # rule has no opinion about which of them to open.
            continue
        for item in group:
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
    return normalized


def _file_role(item: dict[str, Any], known_paths: set[str]) -> str:
    path = str(item.get("path") or item.get("name") or "").replace("\\", "/").casefold()
    if path.endswith(".timeseries.data"):
        return "auxiliary"
    if path.endswith(".wiff.scan"):
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
    value = path.replace("\\", "/").casefold()
    for suffix in (".timeseries.data", ".wiff.scan", ".wiff2", ".wiff"):
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
    value = _slashes(path).casefold()
    return next((suffix for suffix in _ALL_CONTAINER_SUFFIXES if value.endswith(suffix)), "")


def _is_folder_container(segment: str) -> bool:
    value = segment.casefold()
    return any(
        value.endswith(suffix) and len(value) > len(suffix) for suffix in FOLDER_CONTAINER_SUFFIXES
    )


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

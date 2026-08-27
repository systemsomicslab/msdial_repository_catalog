from __future__ import annotations

import csv
import html
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable


class RepositoryHttpClient:
    def __init__(self, timeout: int = 90) -> None:
        self.timeout = timeout
        self.headers = {"User-Agent": "MS-DIAL-Repository-Catalog/0.1"}

    def get_bytes(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers=self.headers)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.read()

    def get_text(self, url: str) -> str:
        return self.get_bytes(url).decode("utf-8-sig", errors="replace")

    def get_json(self, url: str) -> Any:
        return json.loads(self.get_text(url))

    def content_length(self, url: str) -> int:
        request = urllib.request.Request(url, method="HEAD", headers=self.headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return int(response.headers.get("Content-Length") or 0)
        except Exception:
            return 0


def parse_tab_blocks(text: str) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            if current:
                blocks.append(current)
                current = {}
            continue
        key, separator, value = line.partition("\t")
        if separator:
            current[key.strip().lower()] = value.strip()
    if current:
        blocks.append(current)
    return blocks


def parse_records(text: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return parse_tab_blocks(text)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        if all(not isinstance(value, dict) for value in payload.values()):
            return [payload]
        return [item for item in payload.values() if isinstance(item, dict)]
    return []


def assay_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        rows = payload.get("data", {}).get("rows", [])
        return [row for row in rows if isinstance(row, dict)]
    text = str(payload or "")
    lines = [line for line in text.splitlines() if line.strip()]
    return [dict(row) for row in csv.DictReader(lines, delimiter="\t")] if lines else []


def parse_sdrf(text: str) -> list[dict[str, str]]:
    reader = csv.reader(text.splitlines(), delimiter="\t")
    try:
        headers = next(reader)
    except StopIteration:
        return []
    counts: dict[str, int] = {}
    unique: list[str] = []
    for header in headers:
        base = header.strip() or "Unnamed"
        counts[base] = counts.get(base, 0) + 1
        unique.append(base if counts[base] == 1 else f"{base} [{counts[base]}]")
    return [
        {header: values[index].strip() if index < len(values) else "" for index, header in enumerate(unique)}
        for values in reader
        if any(value.strip() for value in values)
    ]


def metadata_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return metadata_scalar(
            value.get("annotationValue") or value.get("value") or value.get("name")
        )
    if isinstance(value, list):
        return "; ".join(filter(None, (metadata_scalar(item) for item in value)))
    return str(value).strip()


def display_field_name(value: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[_-]+", " ", str(value or "")).strip())


def first_matching_value(row: dict[str, Any], names: Iterable[str]) -> str:
    normalized = {str(key).strip().casefold(): value for key, value in row.items()}
    for name in names:
        value = metadata_scalar(normalized.get(name.casefold()))
        if value:
            return value
    return ""


def parse_name_value_pairs(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in re.split(r"\s*\|\s*|\s*;\s*", value.strip()):
        key, separator, item = part.partition(":")
        if not separator:
            key, separator, item = part.partition("=")
        if separator and key.strip():
            result[display_field_name(key)] = item.strip()
    return result


def html_text(value: str) -> str:
    without_scripts = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", without_scripts)))


def infer_separation(*values: Any) -> str:
    text = normalized_text(*values)
    if re.search(r"\b(gc[- /]?ms|gc[- ]?tof|gas chromat)", text):
        return "GC-MS"
    if re.search(r"\b(direct infusion|flow injection|infusion ms|di[- /]?ms)\b", text):
        return "DI-MS"
    if re.search(r"\b(u?plc|hplc|lc)[- /]?(q?q?q?|q?tof|orbitrap|ms)|liquid chromat", text):
        return "LC-MS"
    return "Unknown"


def infer_chromatography(*values: Any) -> str:
    text = normalized_text(*values)
    if re.search(r"\b(hilic|hydrophilic interaction)\b", text):
        return "HILIC"
    if re.search(r"\b(normal phase|normal-phase|np[- _]?lc)\b", text):
        return "Normal phase"
    if re.search(r"\b(reversed? phase|reverse phase|rp[- _]?lc|c18|ods)\b", text):
        return "Reversed phase"
    return "Unknown"


def infer_acquisition(*values: Any) -> str:
    text = normalized_text(*values)
    if re.search(r"\b(mrm|multiple reaction monitoring)\b", text):
        return "MRM"
    if re.search(r"\b(srm|selected reaction monitoring)\b", text):
        return "SRM"
    if re.search(r"\b(sim|selected ion monitoring)\b", text):
        return "SIM"
    if re.search(r"\b(aif|all[- ]?ions?|all ion fragmentation|msall|mse)\b", text):
        return "AIF"
    if re.search(r"\b(dia|swath|data[- ]independent)\b", text):
        return "DIA"
    if re.search(r"\b(dda|auto\s*ms/?ms|data[- ]dependent)\b", text):
        return "DDA"
    if re.search(r"\bfull scan\b|\bscan mode\b", text):
        return "FullScan"
    return "Unknown"


def infer_ion_mode(*values: Any) -> str:
    text = normalized_text(*values)
    if re.search(r"\b(alternating(?: polarity)?|polarity switching|switching polarity)\b", text):
        return "Both"
    positive = bool(re.search(r"\bpos(?:itive)?\b|esi\s*\+", text))
    negative = bool(re.search(r"\bneg(?:ative)?\b|esi\s*-", text))
    if positive and negative:
        return "Both"
    if positive:
        return "Positive"
    if negative:
        return "Negative"
    return "Unknown"


def infer_ion_mobility(*values: Any) -> str:
    text = normalized_text(*values)
    if re.search(r"\b(ion mobility|pasef|tims|drift time|ccs)\b", text):
        if re.search(r"\b(ion mobility\W{0,12}(?:no|off|false|disabled))\b", text):
            return "Disabled"
        return "Enabled"
    return "Unknown"


def infer_omics(*values: Any) -> str:
    text = normalized_text(*values)
    if re.search(r"\blipid(?:ome|omic|omics)?\b", text):
        return "Lipidomics"
    if re.search(r"\bmetabol(?:ome|omic|omics)?\b", text):
        return "Metabolomics"
    return "Unknown"


def infer_untargeted(*values: Any) -> bool | None:
    text = normalized_text(*values)
    untargeted = bool(re.search(r"\b(untargeted|non[- ]?targeted|global metabol|global lipid)\b", text))
    targeted = bool(re.search(r"\b(targeted|mrm|srm|sim|quantitative panel)\b", text))
    if untargeted and targeted:
        return None
    if untargeted:
        return True
    if targeted:
        return False
    return None


def normalized_text(*values: Any) -> str:
    return re.sub(r"[_]+", " ", " ".join(metadata_scalar(value) for value in values).casefold())


def technical_signature(values: dict[str, str]) -> tuple[str, ...]:
    keys = (
        "separation", "chromatography", "ion_mode", "acquisition_mode",
        "ion_mobility", "instrument", "target_omics",
    )
    return tuple(str(values.get(key) or "Unknown").strip().casefold() for key in keys)


def source_evidence(source: str, field: str, value: Any, note: str = "") -> dict[str, Any]:
    return {
        "source": source,
        "source_field": field,
        "source_value": metadata_scalar(value),
        "method": "declared",
        "confidence": 1.0,
        "note": note,
    }


def sidecar_name(name: str) -> bool:
    value = name.casefold()
    return value.endswith((".wiff.scan", ".wiff2.scan"))


def parse_size(value: str) -> int:
    match = re.match(r"\s*([0-9.]+)\s*([kmgt]?)(?:i?b)?\s*$", value, re.IGNORECASE)
    if not match:
        return 0
    scale = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
    return int(float(match.group(1)) * scale[match.group(2).casefold()])


def parse_apache_index(text: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in re.findall(r"(?is)<tr>.*?</tr>", text):
        href_match = re.search(r'(?i)<a\s+href="([^"]+)"', row)
        sizes = re.findall(r'(?i)<td\s+align="right">\s*([0-9.]+\s*[KMGT]?)\s*</td>', row)
        if not href_match or not sizes:
            continue
        href = urllib.parse.unquote(html.unescape(href_match.group(1)))
        if href.endswith("/") or href.startswith("?") or "Parent Directory" in row:
            continue
        size = parse_size(sizes[-1])
        result[href] = size
        result.setdefault(Path(href).name, size)
    return result


def extract_publications(*values: Any) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for value in values:
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            lowered = {str(key).casefold(): item for key, item in candidate.items()}
            doi = metadata_scalar(lowered.get("doi") or lowered.get("publication doi"))
            pubmed = metadata_scalar(lowered.get("pubmedid") or lowered.get("pubmed id") or lowered.get("pmid"))
            title = metadata_scalar(lowered.get("title") or lowered.get("publication title") or lowered.get("citation"))
            if doi or pubmed or title:
                records.append({"title": title, "doi": doi, "pubmed_id": pubmed})
    joined = json.dumps(values, ensure_ascii=False)
    for doi in re.findall(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", joined, re.IGNORECASE):
        cleaned = doi.rstrip(".,;)]}")
        if not any(item["doi"].casefold() == cleaned.casefold() for item in records):
            records.append({"title": "", "doi": cleaned, "pubmed_id": ""})
    return records

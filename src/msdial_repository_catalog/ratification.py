"""A campaign approval standing in for the confirmation that saves a Class proposal or an abstention.

WHY THIS EXISTS. Saving a Class decision is confirmation boundary 3: a person reads the grouping, or the
abstention, and says yes, in the conversation. A campaign of several hundred units cannot ask that of
anyone once per unit, so one approval, given once by a person, of one campaign manifest digest covers
boundary 3 for the units it names (Interactive's campaign_authorization.py, schema
msdial-campaign-authorization.v1). This module is how that approval reaches a saved proposal.

WHAT A RATIFICATION IS. The approval id and the manifest digest of that record, and optionally the
sha256 of the authorization file as the runner read it and the proposal id the approved manifest names
for the unit. The Catalog does not read the authorization record: it has no dependency on Interactive,
and the runner validates the record for every boundary it crosses. What the Catalog guarantees is that
a ratified save says which approval ratified it, in a form an audit can read beside the proposal, and
that a ratification which does not name one is refused rather than taken for a confirmation.

WHAT IT NEVER DOES. A ratification that lacks the approval id or the digest, or carries a digest that is
not 'sha256:' and 64 lowercase hex digits (the form Interactive accepts), is refused whole; it is never
quietly dropped in favour of an unratified save. With no ratification passed nothing changes: saving
still needs confirmed=true.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any


# A proposal reaches the MCP layer reading "proposed", which is what ClassProposal is born as.
# Saving is gated on an explicit confirmation, and that confirmation was the only record that
# anyone had agreed to the grouping -- it lived in a conversation and in nothing an audit could
# read. A machine-authored grouping executed and published beside a proposal still reading
# "proposed" is the whole of the safety argument missing.
ACCEPTED_STATUS = "accepted"

RATIFICATION_KIND = "campaign_approval"
AUTHORIZATION_SCHEMA = "msdial-campaign-authorization.v1"
CLASS_BOUNDARY = 3
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ACCEPTED_KEYS = frozenset(
    {"approval_id", "manifest_digest", "authorization_sha256", "campaign_id", "proposal_id"}
)


class RatificationError(ValueError):
    """A ratification was passed and cannot stand in for the confirmation."""

    def __init__(self, codes: list[str], reasons: list[str]) -> None:
        self.codes = list(dict.fromkeys(codes))
        self.reasons = list(reasons)
        super().__init__(f"ratification_refused [{', '.join(self.codes)}]: " + " ".join(self.reasons))


def normalize_ratification(value: Any) -> dict[str, Any] | None:
    """The checked ratification, None when none was passed, or RatificationError."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise RatificationError(["malformed"], ["A ratification is an object naming a campaign approval."])
    codes: list[str] = []
    reasons: list[str] = []
    unknown = sorted(str(key) for key in value if str(key) not in _ACCEPTED_KEYS)
    if unknown:
        codes.append("unknown_keys")
        reasons.append(
            f"A ratification names an approval and nothing else; {', '.join(unknown)} is not one of "
            f"{', '.join(sorted(_ACCEPTED_KEYS))}."
        )
    text = {key: str(value.get(key) or "").strip() for key in _ACCEPTED_KEYS}
    if not text["approval_id"]:
        codes.append("approval_id")
        reasons.append("The ratification names no campaign approval id.")
    if not text["manifest_digest"]:
        codes.append("manifest_digest")
        reasons.append("The ratification names no campaign manifest digest.")
    elif not _DIGEST.fullmatch(text["manifest_digest"]):
        codes.append("manifest_digest")
        reasons.append("The manifest digest is not 'sha256:' followed by 64 lowercase hex digits.")
    if text["authorization_sha256"] and not _SHA256.fullmatch(text["authorization_sha256"]):
        codes.append("authorization_sha256")
        reasons.append("The authorization sha256 is not 64 lowercase hex digits.")
    if codes:
        raise RatificationError(codes, reasons)
    return {
        "kind": RATIFICATION_KIND,
        "authorization_schema": AUTHORIZATION_SCHEMA,
        "boundary": CLASS_BOUNDARY,
        "approval_id": text["approval_id"],
        "manifest_digest": text["manifest_digest"],
        "authorization_sha256": text["authorization_sha256"],
        "campaign_id": text["campaign_id"],
        "proposal_id": text["proposal_id"],
    }


def ratification_record(ratification: Any, proposal_id: str, *, abstention: bool) -> dict[str, Any]:
    """The record stored with the proposal it ratifies, from the ratification as it was passed.

    A ratification that names a proposal id ratifies that grouping only. The approved manifest pins the
    Class digest of each unit, and a proposal id is a hash of its payload, so a different id here is a
    grouping the person never approved: it is refused, not saved under their approval.
    """
    checked = normalize_ratification(ratification)
    if checked is None:
        raise RatificationError(["malformed"], ["A ratification is an object naming a campaign approval."])
    if checked["proposal_id"] and checked["proposal_id"] != proposal_id:
        raise RatificationError(
            ["proposal_mismatch"],
            [
                f"The campaign approved Class proposal {checked['proposal_id']} for this unit, and the "
                f"proposal being saved is {proposal_id}."
            ],
        )
    return {
        **checked,
        "proposal_id": proposal_id,
        "abstention": bool(abstention),
        "ratified_at": datetime.now(timezone.utc).isoformat(),
    }

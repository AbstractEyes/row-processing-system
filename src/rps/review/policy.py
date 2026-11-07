"""The review protocol as pure functions.

Who reviews a row, and when a row is settled, depend only on the row's flags, its
verdicts so far, the reviewers' families and the policy. Nothing here touches a
store, so the rules can be tested on their own.

The protocol, with the default policy:

1. The first review goes to a reviewer from a family other than the writer's,
   chosen round-robin across the available reviewers.
2. A "keep" with no assessor flag accepts the row.
3. A "fix" or "reject", or an assessor flag, puts the row in question: it goes to
   a further reviewer from a family not yet used on it.
4. Two "keep" verdicts accept an in-question row; two "reject" verdicts discard it.
5. After three reviewers without a settlement the row goes to the owner's hand queue.
6. A share of accepted rows (the audit sample) gets one more review from another
   family. The audit changes nothing unless it rejects the row, which puts the row
   back in question.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Sequence

from ..config import ReviewerConfig, ReviewPolicy

NEED_FIRST = "need_first"
ACCEPT = "accept"
DISCARD = "discard"
FURTHER = "further"
HAND = "hand"


@dataclass(frozen=True)
class Settlement:
    """What the verdicts so far mean for a row. `outcome` is one of:

    need_first  no verdict yet
    accept      the row is accepted
    discard     the row is discarded
    further     the row is in question and needs another reviewer
    hand        reviewers could not settle it; it goes to the owner
    """

    outcome: str
    reason: str


def settle(policy: ReviewPolicy, *, flagged: bool, verdicts: Sequence[str], reopened: bool = False) -> Settlement:
    """Apply the policy to a row's verdicts, in the order they arrived.

    `flagged` is True when the assessor flagged the row. `reopened` is True for a
    row an audit reviewer rejected after it was accepted; its first-keep shortcut no
    longer applies.
    """
    total = len(verdicts)
    keeps = sum(1 for verdict in verdicts if verdict == "keep")
    rejects = sum(1 for verdict in verdicts if verdict == "reject")
    if total == 0:
        return Settlement(NEED_FIRST, "no verdict yet")
    if keeps >= policy.keeps_to_accept:
        return Settlement(ACCEPT, f"{keeps} keep verdicts")
    if rejects >= policy.rejects_to_discard:
        return Settlement(DISCARD, f"{rejects} reject verdicts")
    if policy.first_keep_accepts and not flagged and not reopened and total == 1 and verdicts[0] == "keep":
        return Settlement(ACCEPT, "keep with no assessor flag")
    if total >= policy.max_reviewers:
        return Settlement(HAND, f"{total} reviewers without a settlement")
    return Settlement(FURTHER, "in question")


def pick_first_reviewer(
    reviewers: Sequence[ReviewerConfig], writer_family: str, ordinal: int
) -> ReviewerConfig | None:
    """Choose the first reviewer: round-robin over the reviewers outside the writer's family.

    `reviewers` are the available reviewers in a fixed order. `ordinal` counts the
    rows from the same writer that reached review before this one, so consecutive
    rows go to consecutive reviewers. The answer depends only on its arguments, which
    keeps the rotation exact across a restart. Returns None when every reviewer is
    from the writer's family.
    """
    eligible = [reviewer for reviewer in reviewers if reviewer.family != writer_family]
    if not eligible:
        return None
    return eligible[ordinal % len(eligible)]


def _spread(key: str) -> int:
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16)


def pick_further_reviewer(
    reviewers: Sequence[ReviewerConfig], used_families: set[str], writer_family: str, key: str
) -> ReviewerConfig | None:
    """Choose a reviewer from a family not yet used on the row.

    Families other than the writer's are preferred; the writer's own family is used
    only when no other unused family is left. Among equals the choice is spread by a
    hash of `key`, so the same row always gets the same answer.
    """
    unused = [reviewer for reviewer in reviewers if reviewer.family not in used_families]
    if not unused:
        return None
    preferred = [reviewer for reviewer in unused if reviewer.family != writer_family] or unused
    return preferred[_spread(key) % len(preferred)]


def audit_selected(row_id: str, fraction: float, seed: str) -> bool:
    """Whether a row is in the audit sample.

    The sample is a hash of the seed and the row id, so it looks random but is the
    same on every run: a restart never audits a different set of rows.
    """
    if fraction <= 0:
        return False
    if fraction >= 1:
        return True
    value = int(hashlib.sha256(f"{seed}:{row_id}".encode("utf-8")).hexdigest()[:8], 16) / 0x100000000
    return value < fraction

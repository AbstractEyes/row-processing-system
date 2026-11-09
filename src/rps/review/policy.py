"""The review protocol as pure functions.

Who reviews a row, and when a row is settled, depend only on the row's flags, its
verdicts so far, the reviewers' families and the policy. Nothing here touches a
store, so the rules can be tested on their own.

The protocol, with the default policy:

1. The first review goes to a reviewer from a family other than the writer's,
   chosen round-robin across the available reviewers. (The first review of a row that was
   written again after a reviewer sent it back prefers a reviewer that did not review the row
   it replaces, when there is one: see `rps.repair`.)
2. A "keep" with no assessor flag accepts the row, unless it comes from a reviewer
   that may not keep alone (`solo_keep` off): that keep never accepts a row by itself,
   so the row is in question, as a flagged row is.
3. A "fix" or "reject", or an assessor flag, puts the row in question: it goes to
   a further reviewer from a family not yet used on it. With `fix_to_hand` on, a "fix"
   sends the row to the owner's hand queue at once instead (rule 4 is checked first).
4. Two "keep" verdicts accept an in-question row, as long as one of them comes from a
   reviewer that may keep alone; two "reject" verdicts discard it.
5. After three reviewers without a settlement the row goes to the owner's hand queue.
6. A share of accepted rows (the audit sample) gets one more review from another
   family. The audit changes nothing unless it rejects the row, which puts the row
   back in question.

A row also goes to the hand queue when no reviewer can take it: no reviewer from another
family than the writer's for a first review, or none from a family not yet used for a further
one. Those two reasons are named below, because a row sent for them is looked at again: it is
reopened when the policy as it stands now still asks for that review of the row and the
configuration has a reviewer that can take it (see `rps.controller`).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Collection, Sequence

from ..config import ReviewerConfig, ReviewPolicy

NEED_FIRST = "need_first"
ACCEPT = "accept"
DISCARD = "discard"
FURTHER = "further"
HAND = "hand"

# The reasons a row is sent to the hand queue for want of a reviewer, and what a row that is
# looked at again for one of them is told when a reviewer can take it.
NO_FIRST_REVIEWER = "no reviewer from another family than the writer is available"
NO_FURTHER_REVIEWER = "no reviewer from an unused family is available"
REOPEN_REASONS: dict[str, str] = {
    NO_FIRST_REVIEWER: "a reviewer from another family than the writer is now available",
    NO_FURTHER_REVIEWER: "a reviewer from an unused family is now available",
}
# Why a row with a "fix" verdict goes to the hand queue when the policy's `fix_to_hand` is on.
FIX_TO_HAND_REASON = "a fix verdict: the row waits for the owner"


@dataclass(frozen=True)
class Settlement:
    """What the verdicts so far mean for a row. `outcome` is one of:

    need_first  no verdict yet
    accept      the row is accepted
    discard     the row is discarded
    further     the row is in question and needs another reviewer
    hand        the row goes to the owner: reviewers could not settle it, or it has a
                "fix" verdict and the policy's `fix_to_hand` is on
    """

    outcome: str
    reason: str


def settle(
    policy: ReviewPolicy,
    *,
    flagged: bool,
    verdicts: Sequence[str],
    reopened: bool = False,
    solo: Sequence[bool] | None = None,
) -> Settlement:
    """Apply the policy to a row's verdicts, in the order they arrived.

    `flagged` is True when the assessor flagged the row. `reopened` is True for a
    row an audit reviewer rejected after it was accepted; its first-keep shortcut no
    longer applies. `solo`, when given, says for each verdict, in the same order, whether
    its reviewer may keep alone (the reviewer's `solo_keep`). A keep from a reviewer that
    may not never accepts a row without a keep from one that may: a row whose keeps all
    come from such reviewers stays in question, however many they are, and goes on to a
    further reviewer. Left out, every reviewer may keep alone.

    The order of the rules is the order of the checks: enough keeps accept the row, enough
    rejects discard it, a single keep with no flag accepts it, and only then does the
    policy's `fix_to_hand` (any "fix" verdict hands the row to the owner at once) and its
    `max_reviewers` (the row is handed to the owner after that many reviewers) apply. So
    a row that two keeps or two rejects settle is settled whatever else it was told.
    """
    total = len(verdicts)
    if solo is not None and len(solo) != total:
        raise ValueError(f"solo has {len(solo)} entries for {total} verdicts")
    keeps = rejects = fixes = keeps_alone = 0
    for index, verdict in enumerate(verdicts):
        if verdict == "keep":
            keeps += 1
            if solo is None or solo[index]:
                keeps_alone += 1
        elif verdict == "reject":
            rejects += 1
        elif verdict == "fix":
            fixes += 1
    if total == 0:
        return Settlement(NEED_FIRST, "no verdict yet")
    if keeps >= policy.keeps_to_accept and keeps_alone > 0:
        return Settlement(ACCEPT, f"{keeps} keep verdicts")
    if rejects >= policy.rejects_to_discard:
        return Settlement(DISCARD, f"{rejects} reject verdicts")
    if policy.first_keep_accepts and not flagged and not reopened and total == 1 and verdicts[0] == "keep" and keeps_alone > 0:
        return Settlement(ACCEPT, "keep with no assessor flag")
    if policy.fix_to_hand and fixes > 0:
        return Settlement(HAND, FIX_TO_HAND_REASON)
    only_unsure_keeps = keeps > 0 and keeps_alone == 0  # keeps, and every one from a reviewer that may not keep alone
    if total >= policy.max_reviewers:
        reason = f"{total} reviewers without a settlement"
        if only_unsure_keeps and keeps >= policy.keeps_to_accept:
            reason += " (every keep came from a reviewer that may not keep alone)"
        return Settlement(HAND, reason)
    if only_unsure_keeps:
        return Settlement(FURTHER, "in question (every keep so far came from a reviewer that may not keep alone)")
    return Settlement(FURTHER, "in question")


def pick_first_reviewer(
    reviewers: Sequence[ReviewerConfig], writer_family: str, ordinal: int, avoid: Collection[str] = ()
) -> ReviewerConfig | None:
    """Choose the first reviewer: round-robin over the reviewers outside the writer's family.

    `reviewers` are the available reviewers in a fixed order. `ordinal` counts the
    rows from the same writer that reached review before this one, so consecutive
    rows go to consecutive reviewers. The answer depends only on its arguments, which
    keeps the rotation exact across a restart. Returns None when every reviewer is
    from the writer's family.

    `avoid` names reviewers (by id) that are better left out when another is available: a row that
    was written again after a reviewer sent it back is given, for its first review, to a reviewer
    that did not review the row it replaces, unless every reviewer outside the writer's family did.
    """
    eligible = [reviewer for reviewer in reviewers if reviewer.family != writer_family]
    if not eligible:
        return None
    fresh = [reviewer for reviewer in eligible if reviewer.id not in avoid]
    chosen = fresh or eligible
    return chosen[ordinal % len(chosen)]


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

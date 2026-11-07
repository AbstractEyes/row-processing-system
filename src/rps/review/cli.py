"""Command line for an attended reviewer (a person or an agent session).

    python -m rps.review pull    --reviewer ID --config PATH --into DIR
    python -m rps.review status  --reviewer ID --config PATH --into DIR
    python -m rps.review verdict --packet P --row R --verdict keep|fix|reject --reasons a,b --notes TEXT
    python -m rps.review check   --packet P [--file PATH]
    python -m rps.review push    --packet P --config PATH

`--into` is the local folder that holds pulled packets (default: the current folder).
Notes should name the problem and where it is, such as the field or thought number;
a note over the length limit is refused. Reviewers return verdicts only, never an
edited row.

Exit codes: 0 done, 1 invalid input, 2 refused (for example pushing a packet that
still has rows without verdicts), 3 the store or configuration failed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence, TextIO

from ..config import Config, ConfigError
from ..runtime import build_state_store
from ..store.base import StoreError
from .client import LocalPacket, ReviewerClient, ReviewError, read_local_packet
from .packets import MAX_NOTES_LENGTH, PacketError

EXIT_OK, EXIT_INVALID, EXIT_REFUSED, EXIT_FAILED = 0, 1, 2, 3
PROG = "python -m rps.review"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description="Review packets and record verdicts.")
    commands = parser.add_subparsers(dest="command", required=True)

    pull = commands.add_parser("pull", help="copy the next packet for a reviewer into a local folder")
    pull.add_argument("--reviewer", required=True)
    pull.add_argument("--config", required=True)
    pull.add_argument("--into", required=True)

    status = commands.add_parser("status", help="show packets and rows left")
    status.add_argument("--reviewer", required=True)
    status.add_argument("--config", required=True)
    status.add_argument("--into", default=".")

    verdict = commands.add_parser("verdict", help="record one verdict")
    verdict.add_argument("--packet", required=True)
    verdict.add_argument("--row", required=True)
    verdict.add_argument("--verdict", required=True, choices=["keep", "fix", "reject"])
    verdict.add_argument("--reasons", default="", help="short codes separated by commas (needed for fix and reject)")
    verdict.add_argument(
        "--notes",
        default="",
        help=f"name the problem and where it is (up to {MAX_NOTES_LENGTH} characters)",
    )
    verdict.add_argument("--replace", action="store_true", help="replace an earlier verdict for the row")
    verdict.add_argument("--into", default=".")

    check = commands.add_parser("check", help="validate a verdict file")
    check.add_argument("--packet", required=True)
    check.add_argument("--file", default=None, help="a hand-written verdict file (default: the recorded verdicts)")
    check.add_argument("--into", default=".")

    push = commands.add_parser("push", help="upload the verdicts, then delete the local pictures")
    push.add_argument("--packet", required=True)
    push.add_argument("--config", required=True)
    push.add_argument("--reviewer", default=None, help="optional: must match the packet's reviewer")
    push.add_argument("--into", default=".")
    return parser


def _client_for_packet(into: str, packet_id: str, remote: object = None, reviewer: str | None = None) -> ReviewerClient:
    """A client for the reviewer named in a local packet (the packet says whose it is)."""
    packet = read_local_packet(into, packet_id)
    if packet is None:
        raise ReviewError(f"packet {packet_id} is not in {Path(into).resolve()}; run pull first")
    if reviewer is not None and reviewer != packet.reviewer_id:
        raise ReviewError(f"packet {packet_id} is addressed to {packet.reviewer_id}, not {reviewer}")
    return ReviewerClient(packet.reviewer_id, remote, into)  # type: ignore[arg-type]


def _describe(packet: LocalPacket, client: ReviewerClient, out: TextIO) -> None:
    judged = client.judged_rows(packet)
    out.write(f"packet {packet.packet_id} for {packet.reviewer_id}: {len(packet.row_ids)} rows ({len(judged)} judged, {len(packet.row_ids) - len(judged)} left)\n")
    out.write(f"folder: {packet.folder}\n")
    for row_id in packet.row_ids:
        entry = packet.entry(row_id)
        picture = f" picture: {entry['image']}" if entry.get("image") else ""
        out.write(f"  {row_id}{' (judged)' if row_id in judged else ''}{picture}\n")


def run(argv: Sequence[str] | None = None, out: TextIO | None = None, err: TextIO | None = None) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    args = build_parser().parse_args(argv)
    try:
        if args.command == "pull":
            config = Config.load(args.config)
            client = ReviewerClient(args.reviewer, build_state_store(config), args.into)
            packet, missing = client.pull()
            if packet is None:
                out.write(f"no packets are waiting for {args.reviewer}\n")
                return EXIT_OK
            _describe(packet, client, out)
            for name in missing:
                out.write(f"warning: the picture {name} is not in the shared store\n")
            out.write(f"Read {packet.folder / 'packet.json'}: each row has the content to judge, the task it was written for and a picture when there is one.\n")
            out.write(
                f"Record each verdict: {PROG} verdict --packet {packet.packet_id} --row ROW_ID "
                f'--verdict keep|fix|reject --reasons code1,code2 --notes "name the problem and where it is" --into {args.into}\n'
            )
            out.write(f"When every row has a verdict: {PROG} push --packet {packet.packet_id} --config {args.config} --into {args.into}\n")
            return EXIT_OK

        if args.command == "status":
            config = Config.load(args.config)
            client = ReviewerClient(args.reviewer, build_state_store(config), args.into)
            statuses = client.status()
            out.write(f"reviewer {args.reviewer}\n")
            for status in statuses:
                state = "pushed" if status.pushed else "not pushed"
                out.write(
                    f"packet {status.packet_id}: {status.total} rows, {status.judged} judged, {status.left} left, "
                    f"{state}, {status.images_left} pictures here\n"
                )
            local_ids = {status.packet_id for status in statuses}
            waiting = [pid for pid in client.pending_packet_ids() if pid not in local_ids]
            out.write(f"waiting in the shared store, not pulled yet: {len(waiting)}\n")
            return EXIT_OK

        if args.command == "verdict":
            client = _client_for_packet(args.into, args.packet)
            reasons = [code.strip() for code in args.reasons.split(",") if code.strip()]
            client.record_verdict(args.packet, args.row, args.verdict, reasons, args.notes, replace=args.replace)
            packet = client.require_packet(args.packet)
            judged = len(client.judged_rows(packet))
            out.write(f"recorded {args.verdict} for row {args.row} in packet {args.packet} ({judged} of {len(packet.row_ids)} judged)\n")
            return EXIT_OK

        if args.command == "check":
            client = _client_for_packet(args.into, args.packet)
            report = client.check(args.packet, args.file)
            for problem in report.problems:
                err.write(f"problem: {problem}\n")
            out.write(f"{len(report.judged)} valid verdicts, {len(report.missing)} rows still without a verdict\n")
            return EXIT_OK if report.ok else EXIT_INVALID

        if args.command == "push":
            config = Config.load(args.config)
            client = _client_for_packet(args.into, args.packet, build_state_store(config), args.reviewer)
            result = client.push(args.packet)
            if not result.pushed:
                for reason in result.reasons:
                    err.write(f"not pushed: {reason}\n")
                return EXIT_REFUSED
            out.write(f"pushed the verdicts for packet {args.packet} to review/{client.reviewer_id}/{args.packet}.jsonl; burned {result.burned} local pictures\n")
            return EXIT_OK
    except (ReviewError, PacketError) as error:
        err.write(f"error: {error}\n")
        return EXIT_INVALID
    except (ConfigError, StoreError) as error:
        err.write(f"error: {error}\n")
        return EXIT_FAILED
    err.write("error: unknown command\n")
    return EXIT_INVALID


def main(argv: Sequence[str] | None = None) -> int:
    return run(argv)

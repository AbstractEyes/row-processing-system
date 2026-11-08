"""Parity of the assessor against the original check scripts on a range of written batches.

    python tools/parity.py --prod <folder with the written batches> --legacy <folder with the original scripts> 145 146 ... 162

Reads batch_NNN.rows.jsonl and batch_NNN.specs.jsonl for each batch. The legacy side is the
dataset's batch checker (called from its package) plus each original script run as a
subprocess, its printed lines read back as (code, row id). The assessor side is
`rps.assessor.report`. The table lists, per code, the rows each side flags and every
difference. Never run by the test suite.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thought_seeds import rows as seed_rows  # noqa: E402

from rps import assessor  # noqa: E402

ID = re.compile(r"art-\d{6}")
SERIES = re.compile(r"s-\d{5}")


def run_script(legacy: Path, name: str, batches: list[str]) -> list[str]:
    out = subprocess.run([sys.executable, str(legacy / name), *batches], capture_output=True, text=True, encoding="utf-8")
    return out.stdout.splitlines()


def legacy_flags(legacy: Path, prod: Path, batches: list[str]) -> dict[str, set[str]]:
    """code -> set of row (or series) ids flagged by the original side."""
    found: dict[str, set[str]] = defaultdict(set)
    for b in batches:
        rows_path, specs_path = prod / f"batch_{b}.rows.jsonl", prod / f"batch_{b}.specs.jsonl"
        report = seed_rows.check_rows(str(rows_path), str(specs_path))
        for rid, rule, _msg in report["problems"]:
            if rule == "missing":
                continue
            found[rule.replace("rule ", "rule_").replace(" ", "_")].add(str(rid))
        for rid, _kind, _names in report["warnings"]:
            found["names"].add(str(rid))
        for row in (json.loads(l) for l in rows_path.read_text(encoding="utf-8").splitlines() if l.strip()):
            for step in row["thoughts"]:
                if re.search(r"\b(record|read from|inference|spec)\b", step["text"], re.I):
                    found["formula_word"].add(row["id"])
    section = None
    for line in run_script(legacy, "genre_check.py", batches):
        if line.startswith("off-list genres"):
            section = "genre_off_list"
        elif line.startswith("spec-language hits"):
            section = "spec_language"
        elif line.startswith("advice hits"):
            section = "advice"
        elif line.startswith("series with mixed genres"):
            section = "series_genre"
            continue
        if section == "series_genre":
            found[section].update(SERIES.findall(line))
        elif section:
            found[section].update(ID.findall(line))
    for line in run_script(legacy, "q_check.py", batches):
        if "'medium'" in line or "'deep'" in line:
            found["wording_missing"].update(ID.findall(line))
    for line in run_script(legacy, "dom_check.py", batches):
        found["domain_medium"].update(ID.findall(line))
    for line in run_script(legacy, "spell_check.py", batches):
        if line.startswith("   "):
            found["us_spelling"].update(ID.findall(line))
    for line in run_script(legacy, "garment_check.py", batches):
        ids = ID.findall(line)
        if not ids or not line.startswith("("):
            continue
        for needle, code in (("text lacks", "garment_missing_text"), ("tags lack", "garment_missing_tags"), ("dress cap", "dress_caption"), ("bare jeans", "jeans_tag")):
            if needle in line:
                found[code].add(ids[0])
    for line in run_script(legacy, "style_list.py", batches):
        if line.startswith("lowercase style/period words in PROSE"):
            found["style_lowercase"].update(ID.findall(line))
    return found


levels: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)


def assessor_flags(prod: Path, batches: list[str]):
    found: dict[str, set[str]] = defaultdict(set)
    detail: dict[tuple[str, str], str] = {}
    levels.clear()
    for b in batches:
        rows = [json.loads(l) for l in (prod / f"batch_{b}.rows.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        items = [json.loads(l) for l in (prod / f"batch_{b}.specs.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        for p in assessor.report(rows, items).problems:
            if p.code == "missing":
                continue
            found[p.code].add(p.row_id)
            levels[(p.code, p.severity)].append((p.row_id, p.message))
            detail.setdefault((p.code, p.row_id), f"[{p.severity}] {p.message}")
    return found, detail


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prod", required=True)
    parser.add_argument("--legacy", required=True)
    parser.add_argument("--show", type=int, default=6, help="differences listed per code")
    parser.add_argument("--show-errors", type=int, default=12, help="hits listed per new error-level code")
    parser.add_argument("batches", nargs="+")
    args = parser.parse_args()
    prod, legacy = Path(args.prod), Path(args.legacy)
    old = legacy_flags(legacy, prod, args.batches)
    new, detail = assessor_flags(prod, args.batches)
    rows = 0
    for b in args.batches:
        rows += sum(1 for l in (prod / f"batch_{b}.rows.jsonl").read_text(encoding="utf-8").splitlines() if l.strip())
    print(f"{rows} rows in batches {args.batches[0]}..{args.batches[-1]}\n")
    print(f"{'code':26} {'original':>9} {'assessor':>9} {'both':>6} {'only original':>14} {'only assessor':>14}")
    legacy_codes = sorted(old)
    for code in legacy_codes:
        a, b = old[code], new.get(code, set())
        print(f"{code:26} {len(a):9} {len(b):9} {len(a & b):6} {len(a - b):14} {len(b - a):14}")
    print("\nDifferences")
    for code in legacy_codes:
        a, b = old[code], new.get(code, set())
        for rid in sorted(a - b)[: args.show]:
            print(f"  {code}: only original: {rid}")
        for rid in sorted(b - a)[: args.show]:
            print(f"  {code}: only assessor: {rid} {detail.get((code, rid), '')[:150]}")
    print("\nFindings with no original counterpart (rows flagged)")
    for code in sorted(set(new) - set(legacy_codes)):
        print(f"  {code}: {len(new[code])}")
    print("\nBy severity, with reasons (errors are listed first)")
    for (code, severity), hits in sorted(levels.items(), key=lambda kv: (kv[0][1] != "error", kv[0])):
        if code in legacy_codes:
            continue
        print(f"  {code} [{severity}] rows={len({r for r, _ in hits})}")
        if severity == "error":
            for rid, message in hits[: args.show_errors]:
                print(f"      {rid}: {message[:150]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

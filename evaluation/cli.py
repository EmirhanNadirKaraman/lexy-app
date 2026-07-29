"""Command-line entry point.

::

    python -m evaluation run     --label baseline
    python -m evaluation run     --label joined --corpus benchmark/documents
    python -m evaluation compare out/eval/baseline.json out/eval/joined.json
    python -m evaluation list

Run ``python -m evaluation <cmd> --help`` for the full flag surface.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .adapters import BlockJsonSource, DoclingLayoutJsonSource
from .annotation import discover_document_ids
from .compare import IncomparableRuns, compare_runs, render_comparison
from .report import load_run, render_report, write_report
from .runner import run_evaluation
from .segmentation import DEFAULT_MODEL

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CORPUS = REPO_ROOT / "benchmark" / "documents"
DEFAULT_OUT = REPO_ROOT / "out" / "eval"

#: Where the real, gitignored corpus lives. Overridable so the harness can be
#: pointed at a copy without editing code.
CORPUS_ROOT_ENV = "BENCHMARK_CORPUS_ROOT"


def _build_source(args) -> object:
    if args.source == "blocks":
        return BlockJsonSource(
            directory=Path(args.corpus),
            name=f"blocks_baseline{'_joined' if args.join else '_per_block'}",
            model=args.model,
            drop_bare_numbers=args.drop_bare_numbers,
            join_across_blocks=args.join,
        )
    root = Path(os.environ.get(CORPUS_ROOT_ENV, REPO_ROOT / "files")) / "json"
    return DoclingLayoutJsonSource(
        directory=root,
        name=f"docling_json{'_joined' if args.join else '_per_block'}",
        model=args.model,
        drop_bare_numbers=args.drop_bare_numbers,
        join_across_blocks=args.join,
    )


def cmd_run(args) -> int:
    corpus = Path(args.corpus)
    if not corpus.is_dir():
        print(f"error: corpus directory not found: {corpus}", file=sys.stderr)
        return 2

    source = _build_source(args)
    run = run_evaluation(
        corpus_dir=corpus,
        source=source,
        label=args.label,
        tolerance=args.tolerance,
        model=args.model,
    )

    if not run.results:
        print(
            "error: no documents evaluated — is the source producing output "
            f"for the gold ids in {corpus}?",
            file=sys.stderr,
        )
        for item in run.skipped:
            print(f"  skipped {item['document_id']}: {item['reason']}", file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    json_path = run.write_json(out_dir / f"{args.label}.json")
    report_path = write_report(run, out_dir / f"{args.label}.md")

    print(render_report(run))
    print(f"wrote {json_path}")
    print(f"wrote {report_path}")
    return 0


def cmd_compare(args) -> int:
    before, after = load_run(Path(args.before)), load_run(Path(args.after))
    try:
        text = render_comparison(before, after, strict=not args.allow_mismatch)
    except IncomparableRuns as exc:
        print(f"error: runs are not comparable — {exc}", file=sys.stderr)
        print("       pass --allow-mismatch to diff anyway (numbers may mislead)",
              file=sys.stderr)
        return 2

    print(text)
    if args.json:
        result = compare_runs(before, after, strict=not args.allow_mismatch)
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"wrote {args.json}")
    if args.fail_on_regression:
        result = compare_runs(before, after, strict=not args.allow_mismatch)
        if result["regressed"]:
            print(f"REGRESSED: {', '.join(result['regressed'])}", file=sys.stderr)
            return 1
    return 0


def cmd_list(args) -> int:
    corpus = Path(args.corpus)
    ids = discover_document_ids(corpus)
    if not ids:
        print(f"no gold documents in {corpus}")
        return 0
    print(f"{len(ids)} gold document(s) in {corpus}:")
    for doc_id in ids:
        print(f"  {doc_id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evaluation",
        description="Document-ingestion evaluation harness (roadmap A1).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="evaluate a source against the gold corpus")
    run_p.add_argument("--label", required=True, help="name for this run's artifacts")
    run_p.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    run_p.add_argument("--out", default=str(DEFAULT_OUT))
    run_p.add_argument(
        "--source",
        choices=("blocks", "docling"),
        default="blocks",
        help="'blocks' = committed benchmark; 'docling' = local gitignored corpus",
    )
    run_p.add_argument("--model", default=DEFAULT_MODEL)
    run_p.add_argument("--tolerance", type=int, default=1)
    run_p.add_argument(
        "--join",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="join blocks before segmenting (--no-join segments each block alone, "
        "which is what a block-per-unit reader does today)",
    )
    run_p.add_argument(
        "--drop-bare-numbers",
        action="store_true",
        help="drop blocks that are just a page number",
    )
    run_p.set_defaults(func=cmd_run)

    cmp_p = sub.add_parser("compare", help="diff two run-result JSON files")
    cmp_p.add_argument("before")
    cmp_p.add_argument("after")
    cmp_p.add_argument("--json", help="also write a machine-readable diff here")
    cmp_p.add_argument(
        "--allow-mismatch",
        action="store_true",
        help="diff even when corpus/schema/segmenter differ (numbers may mislead)",
    )
    cmp_p.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="exit 1 if any metric regressed (for CI)",
    )
    cmp_p.set_defaults(func=cmd_compare)

    list_p = sub.add_parser("list", help="list gold documents")
    list_p.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    list_p.set_defaults(func=cmd_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

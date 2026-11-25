"""Ask one grounded question over documents, with verified citations.

Read-only. Documents come from the configured Drive folder through
apm_connectors' drive_list_files/drive_read_file routes, or from a local
folder with --dir (handy for checking a contract before it is in Drive,
or for trying the gates with no connectors server running).

Prints the status (grounded | insufficient_evidence | needs_human_review),
the answer only when grounded, and every citation with its document,
page and whether it verified. Exit code 0 only when grounded.

Usage:
    pip install -e ".[dev,rag]"
    python scripts/ask_docs.py "What notice period is required for non-renewal?" --name-contains "Acme"
    python scripts/ask_docs.py "What is the uplift cap?" --dir ./contracts
    python scripts/ask_docs.py "..." --dir ./contracts --retrieve-only   # no model call

Needs ANTHROPIC_API_KEY (or an `ant auth login` profile) unless
--retrieve-only; RAG_MODEL picks the model (default claude-sonnet-5).
Drive mode also needs APM_CONNECTORS_BASE_URL (+ APM_CONNECTORS_API_KEY).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import sys
from pathlib import Path

from apm_orchestrator.config import load_settings
from apm_orchestrator.rag.answer import DEFAULT_MIN_SCORE, DEFAULT_TOP_K, RagResult, answer_question
from apm_orchestrator.rag.chunking import chunk_documents
from apm_orchestrator.rag.drive_qa import DEFAULT_MAX_FILES, ask_drive
from apm_orchestrator.rag.index import BM25Index
from apm_orchestrator.rag.loaders import UnsupportedDocument, load_bytes
from apm_orchestrator.rag.runtime import build_answer_model


class _RetrieveOnly:
    """Stands in for the model with --retrieve-only: reaching it means the
    abstain gate passed, which is all that mode reports."""

    async def complete(self, system, prompt, schema):  # noqa: ARG002
        raise RuntimeError("retrieve-only: model not called")


def _load_dir(path: Path):
    docs, skipped = [], []
    for f in sorted(p for p in path.rglob("*") if p.is_file()):
        mime = mimetypes.guess_type(f.name)[0] or ""
        try:
            docs.append(load_bytes(str(f.relative_to(path)), f.name, mime, f.read_bytes()))
        except UnsupportedDocument as exc:
            skipped.append({"name": f.name, "reason": str(exc)})
    return docs, skipped


async def _run(args: argparse.Namespace) -> dict:
    model = _RetrieveOnly() if args.retrieve_only else build_answer_model(load_settings())
    if args.dir:
        docs, skipped = _load_dir(Path(args.dir))
        chunks = chunk_documents(docs)
        if not chunks:
            result = RagResult(args.question, "insufficient_evidence", None, reason="no_readable_documents")
        else:
            result = await answer_question(
                args.question, BM25Index(chunks), model, top_k=args.top_k, min_score=args.min_score
            )
        return {
            **result.to_dict(),
            "documents": [{"file_id": d.doc_id, "name": d.name, "pages": len(d.pages)} for d in docs],
            "skipped": skipped,
        }

    from apm_orchestrator.connectors_client import ConnectorsClient

    async with ConnectorsClient() as client:
        return await ask_drive(
            client,
            question=args.question,
            model=model,
            name_contains=args.name_contains,
            max_files=args.max_files,
            top_k=args.top_k,
            min_score=args.min_score,
        )


def _print(out: dict, retrieve_only: bool) -> None:
    if retrieve_only and "retrieve-only: model not called" in (out.get("reason") or ""):
        print("status: abstain gate passed (retrieve-only: the model would be called next)")
    else:
        print(f"status: {out['status']}" + (f"  ({out['reason']})" if out.get("reason") else ""))
    if out.get("answer"):
        print(f"answer: {out['answer']}")
    for c in out["citations"]:
        mark = "ok " if c["ok"] else f"FAILED ({c['reason']})"
        print(f"  [{mark}] {c.get('doc_name')} p.{c.get('page')} {c['chunk_id']}: \"{c['quote']}\"")
    print("retrieved:", ", ".join(f"{r['chunk_id']} (rel {r['relevance']})" for r in out["retrieved"]) or "-")
    for s in out.get("skipped", []):
        print(f"  skipped {s['name']}: {s['reason']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("question")
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--name-contains", help="Drive: only files whose name contains this")
    src.add_argument("--dir", help="read documents from this local folder instead of Drive")
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE)
    parser.add_argument("--retrieve-only", action="store_true", help="run retrieval + abstain gate only; no model call")
    parser.add_argument("--json", action="store_true", help="print the raw result as JSON")
    args = parser.parse_args()

    out = asyncio.run(_run(args))
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        _print(out, args.retrieve_only)
    sys.exit(0 if out["status"] == "grounded" else 1)


if __name__ == "__main__":
    main()

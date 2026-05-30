"""JSON namespace translator for landing/i18n/locales/<locale>/<ns>.json.

Re-uses :mod:`scripts.i18n_translate` batch primitives (LLM call, glossary,
quota guard, mock fallback) but operates on flat dict-of-strings JSON
catalogs (i18next) instead of Fluent FTL.

CLI::

    .venv/bin/python -m scripts.i18n_translate_json \\
        --source landing/i18n/locales/en-SG \\
        --target id-ID \\
        --model claude-haiku-4-5-20251001

Each ``*.json`` file under ``--source`` becomes ``locales/<target>/<ns>.json``
with the same keys, ICU MessageFormat preserved verbatim.

Like the FTL twin, falls back to deterministic mock when no
``ANTHROPIC_API_KEY`` is set so CI / Phase-2 stub runs still produce files.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.i18n_translate import (  # noqa: E402
    BATCH_SIZE,
    DEFAULT_MODEL,
    FluentString,
    TMCache,
    _llm_translate_batch,
    _wait_if_paused_async,
)

logger = logging.getLogger("i18n_translate_json")

DEFAULT_SOURCE = REPO_ROOT / "landing" / "i18n" / "locales" / "en-SG"
DEFAULT_OUT_ROOT = REPO_ROOT / "landing" / "i18n" / "locales"


@dataclass
class JsonNamespace:
    name: str          # "common"
    path: Path         # source en-SG/common.json
    data: dict[str, str]


def _load_namespaces(source_dir: Path) -> list[JsonNamespace]:
    out: list[JsonNamespace] = []
    for p in sorted(source_dir.glob("*.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            logger.warning("Skipping %s — not a flat dict", p)
            continue
        out.append(JsonNamespace(name=p.stem, path=p, data=data))
    return out


async def _translate_dict(
    data: dict[str, str],
    target_locale: str,
    *,
    model: str,
    tm: TMCache,
    glossary_terms,
    batch_size: int = BATCH_SIZE,
    dry_run: bool = False,
) -> dict[str, str]:
    from scripts.i18n_glossary import format_for_prompt, terms_appearing_in

    items: list[FluentString] = [
        FluentString(key=k, text=str(v), pattern=None, is_attribute=False)
        for k, v in data.items()
    ]
    out: dict[str, str] = {}
    pending: list[FluentString] = []
    for it in items:
        cached = await tm.get(it.text, target_locale)
        if cached is not None:
            out[it.key] = cached
        else:
            pending.append(it)

    for i in range(0, len(pending), batch_size):
        batch = pending[i : i + batch_size]
        joined = "\n".join(b.text for b in batch)
        applicable = terms_appearing_in(joined, glossary_terms)
        gloss_block = format_for_prompt(applicable)
        # Mandatory quota guard before every LLM batch.
        await _wait_if_paused_async(max_wait_seconds=3600)
        if dry_run:
            for b in batch:
                out[b.key] = b.text
            continue
        raw = await _llm_translate_batch(
            batch, target_locale, gloss_block, model=model
        )
        by_key = {r.get("key"): r for r in raw if isinstance(r, dict)}
        for b in batch:
            row = by_key.get(b.key) or {}
            tr = str(row.get("translation") or b.text)
            await tm.set(b.text, target_locale, tr)
            out[b.key] = tr
    return out


async def translate_namespace_dir(
    source_dir: Path,
    target_locale: str,
    *,
    model: str = DEFAULT_MODEL,
    out_root: Path = DEFAULT_OUT_ROOT,
    dry_run: bool = False,
) -> dict[str, int]:
    """Translate every *.json namespace under ``source_dir``.

    Returns ``{namespace: translated_key_count}``.
    """
    from scripts.i18n_glossary import load_glossary

    glossary_terms = load_glossary(target_locale)
    tm = TMCache()
    summary: dict[str, int] = {}

    out_dir = out_root / target_locale
    out_dir.mkdir(parents=True, exist_ok=True)

    for ns in _load_namespaces(source_dir):
        translated = await _translate_dict(
            ns.data,
            target_locale,
            model=model,
            tm=tm,
            glossary_terms=glossary_terms,
            dry_run=dry_run,
        )
        out_path = out_dir / f"{ns.name}.json"
        out_path.write_text(
            json.dumps(translated, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        summary[ns.name] = len(translated)
        logger.info(
            "[%s] %s → %d keys → %s", target_locale, ns.name, len(translated), out_path
        )
    return summary


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="KiX i18next JSON catalog translator (LLM-powered)"
    )
    p.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    p.add_argument("--target", help="Single target locale (e.g. id-ID)")
    p.add_argument("--locales", help="Comma-separated target locales")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args(argv)


def _resolve_locales(args: argparse.Namespace) -> list[str]:
    if args.locales:
        return [s.strip() for s in args.locales.split(",") if s.strip()]
    if args.target:
        return [args.target]
    raise SystemExit("--target or --locales is required")


async def _cli_async(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )
    if not args.source.exists():
        print(f"Source dir not found: {args.source}", file=sys.stderr)
        return 2
    for locale in _resolve_locales(args):
        summary = await translate_namespace_dir(
            args.source, locale, model=args.model, dry_run=args.dry_run
        )
        total = sum(summary.values())
        print(f"[{locale}] {total} keys across {len(summary)} namespaces: {summary}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_cli_async(args))


if __name__ == "__main__":
    sys.exit(main())

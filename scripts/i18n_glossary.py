"""i18n glossary / terminology manager.

The glossary is the source-of-truth for *what the LLM translator must
NOT change* and *what the locale-canonical UI label is*. It is loaded
by :mod:`scripts.i18n_translate` and injected into every batch prompt.

Categories
==========
``product_name``   KiX, KiX ID, Soul, ELTM — never translate.
``technical``      voucher, campaign, attribution — locale-translatable
                   but glossary fixes the canonical translation so it is
                   consistent everywhere.
``brand_specific`` Toast Box, Ya Kun, CHIR CHIR, Lazada — never translate.
``ui_label``       Login, Cancel, Save — locale-specific canonical
                   translations the LLM should reuse verbatim.

File layout
===========
``app/i18n/glossary/global.json``      do-not-translate + technical (universal)
``app/i18n/glossary/<locale>.json``    per-locale ui_label translations

Public API
==========
``load_glossary(locale)``           merged view, locale-aware.
``terms_appearing_in(text, locale)`` slice for a single batch prompt.
``upsert_term(term_id, **fields)``  admin add/update (writes JSON).

CLI:
    python -m scripts.i18n_glossary --list
    python -m scripts.i18n_glossary --list --locale zh-Hans-SG
    python -m scripts.i18n_glossary --add kix_pay --source-term "KiX Pay" --dnt
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
GLOSSARY_DIR = REPO_ROOT / "app" / "i18n" / "glossary"

logger = logging.getLogger("i18n_glossary")

# ── Public dataclass ─────────────────────────────────────────────────────


@dataclass
class GlossaryTerm:
    term_id: str
    source_term: str
    do_not_translate: bool = False
    category: str = "other"
    translation: str | None = None  # only set on per-locale rows
    locale: str | None = None       # only set on per-locale rows

    def to_dict(self) -> dict[str, Any]:
        d = {
            "term_id": self.term_id,
            "source_term": self.source_term,
            "do_not_translate": self.do_not_translate,
            "category": self.category,
        }
        if self.translation is not None:
            d["translation"] = self.translation
        if self.locale is not None:
            d["locale"] = self.locale
        return d


# ── Loaders ──────────────────────────────────────────────────────────────


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_global_terms(glossary_dir: Path | None = None) -> list[GlossaryTerm]:
    """Load the global do-not-translate + technical glossary."""
    d = glossary_dir or GLOSSARY_DIR
    raw = _load_json(d / "global.json")
    out: list[GlossaryTerm] = []
    for t in raw.get("terms", []):
        out.append(
            GlossaryTerm(
                term_id=t["term_id"],
                source_term=t["source_term"],
                do_not_translate=bool(t.get("do_not_translate", False)),
                category=t.get("category", "other"),
            )
        )
    return out


def load_locale_terms(
    locale: str, glossary_dir: Path | None = None
) -> list[GlossaryTerm]:
    """Load per-locale ui_label canonical translations."""
    d = glossary_dir or GLOSSARY_DIR
    raw = _load_json(d / f"{locale}.json")
    out: list[GlossaryTerm] = []
    for t in raw.get("terms", []):
        out.append(
            GlossaryTerm(
                term_id=t["term_id"],
                source_term=t["source_term"],
                do_not_translate=bool(t.get("do_not_translate", False)),
                category=t.get("category", "ui_label"),
                translation=t.get("translation"),
                locale=locale,
            )
        )
    return out


def load_glossary(
    locale: str | None = None, glossary_dir: Path | None = None
) -> list[GlossaryTerm]:
    """Return merged glossary: global + (optional) per-locale.

    Per-locale rows can shadow global ones — handy when a locale needs a
    different ``do_not_translate`` decision (rare, but possible).
    """
    by_id: dict[str, GlossaryTerm] = {}
    for t in load_global_terms(glossary_dir):
        by_id[t.term_id] = t
    if locale:
        for t in load_locale_terms(locale, glossary_dir):
            by_id[t.term_id] = t
    return list(by_id.values())


def terms_appearing_in(
    text: str, terms: Iterable[GlossaryTerm]
) -> list[GlossaryTerm]:
    """Filter glossary to entries whose ``source_term`` appears in ``text``.

    Case-insensitive substring match; long terms checked first so
    "KiX ID" wins over "KiX".
    """
    sorted_terms = sorted(terms, key=lambda t: -len(t.source_term))
    lo = text.lower()
    return [t for t in sorted_terms if t.source_term.lower() in lo]


# ── Admin / mutation ─────────────────────────────────────────────────────


def upsert_term(
    term_id: str,
    *,
    source_term: str | None = None,
    do_not_translate: bool | None = None,
    category: str | None = None,
    translation: str | None = None,
    locale: str | None = None,
    glossary_dir: Path | None = None,
) -> GlossaryTerm:
    """Idempotent upsert into either global.json (locale=None) or
    ``<locale>.json`` (locale set).
    """
    d = glossary_dir or GLOSSARY_DIR
    d.mkdir(parents=True, exist_ok=True)
    path = d / ("global.json" if locale is None else f"{locale}.json")
    raw = _load_json(path) or {
        "_meta": {"locale": locale, "version": 1},
        "terms": [],
    }
    if "terms" not in raw:
        raw["terms"] = []

    found_idx = None
    for i, t in enumerate(raw["terms"]):
        if t.get("term_id") == term_id:
            found_idx = i
            break

    new_row: dict[str, Any] = {"term_id": term_id}
    if found_idx is not None:
        new_row.update(raw["terms"][found_idx])
    if source_term is not None:
        new_row["source_term"] = source_term
    if do_not_translate is not None:
        new_row["do_not_translate"] = bool(do_not_translate)
    if category is not None:
        new_row["category"] = category
    if translation is not None:
        new_row["translation"] = translation

    # Backfill required fields
    new_row.setdefault("source_term", term_id)
    new_row.setdefault("do_not_translate", False)
    new_row.setdefault("category", "ui_label" if locale else "other")

    if found_idx is not None:
        raw["terms"][found_idx] = new_row
    else:
        raw["terms"].append(new_row)

    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return GlossaryTerm(
        term_id=new_row["term_id"],
        source_term=new_row["source_term"],
        do_not_translate=bool(new_row.get("do_not_translate", False)),
        category=new_row.get("category", "other"),
        translation=new_row.get("translation"),
        locale=locale,
    )


def delete_term(
    term_id: str,
    *,
    locale: str | None = None,
    glossary_dir: Path | None = None,
) -> bool:
    """Remove a term. Returns True if anything was removed."""
    d = glossary_dir or GLOSSARY_DIR
    path = d / ("global.json" if locale is None else f"{locale}.json")
    raw = _load_json(path)
    if not raw or "terms" not in raw:
        return False
    before = len(raw["terms"])
    raw["terms"] = [t for t in raw["terms"] if t.get("term_id") != term_id]
    if len(raw["terms"]) == before:
        return False
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


# ── Prompt-injection helper ──────────────────────────────────────────────


def format_for_prompt(terms: Iterable[GlossaryTerm]) -> str:
    """Render glossary entries as a compact prompt block.

    Layout intentionally tight — every additional token is multiplied
    by 3000 strings × N locales when translating at scale.
    """
    lines: list[str] = []
    for t in terms:
        if t.do_not_translate:
            lines.append(f'- "{t.source_term}"  → KEEP AS-IS (do not translate)')
        elif t.translation:
            lines.append(f'- "{t.source_term}"  → "{t.translation}"  (canonical)')
        else:
            # Technical term without locked translation
            lines.append(f'- "{t.source_term}"  (technical term, use consistent translation)')
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────


def _cli(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="KiX i18n glossary manager")
    p.add_argument("--list", action="store_true", help="List all terms (optionally for --locale)")
    p.add_argument("--locale", help="Per-locale glossary (e.g. zh-Hans-SG)")
    p.add_argument("--add", metavar="TERM_ID", help="Insert/update a term")
    p.add_argument("--source-term", help="Source-term value for --add")
    p.add_argument("--translation", help="Translation value (locale-scoped)")
    p.add_argument("--category", default=None, help="product_name|technical|brand_specific|ui_label")
    p.add_argument("--dnt", action="store_true", help="Mark do_not_translate=true")
    p.add_argument("--delete", metavar="TERM_ID", help="Remove a term")
    args = p.parse_args(argv)

    if args.add:
        term = upsert_term(
            args.add,
            source_term=args.source_term or args.add,
            do_not_translate=True if args.dnt else None,
            category=args.category,
            translation=args.translation,
            locale=args.locale,
        )
        print(json.dumps(term.to_dict(), ensure_ascii=False, indent=2))
        return 0

    if args.delete:
        ok = delete_term(args.delete, locale=args.locale)
        print(f"deleted={ok} term_id={args.delete} locale={args.locale}")
        return 0

    if args.list:
        terms = load_glossary(args.locale)
        for t in terms:
            print(json.dumps(t.to_dict(), ensure_ascii=False))
        return 0

    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(_cli())

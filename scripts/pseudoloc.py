"""Pseudolocalisation generator — i18n QA tool.

Generates "fake locale" catalog files that look like a translation but are
mechanically derived from the source. Catches three bug classes BEFORE
real translators see the strings:

  1. Hardcoded English that wasn't extracted (stays plain ASCII in screenshots).
  2. Layout breakage on +30 % string expansion (German is +30 %, Finnish +50 %).
  3. Truncation on narrow CJK fonts reading source-locale strings (+200 %).

Three modes:
  * ``AC`` (Accented) — replace ASCII letters with diacritic counterparts,
    wrap in ``[ … ]``, pad to 130 % length. Pseudo-locale code: ``xx-AC``.
  * ``HA`` (Hash) — prefix every character with ``#``. Pseudo-locale: ``xx-HA``.
  * ``LO`` (Length-only) — duplicate string to ~200 %. Pseudo-locale: ``xx-LO``.

ICU MessageFormat tokens (``{var}``, ``{count, plural, …}``, selector
keywords ``one``/``other``/``=0`` etc.) are preserved untouched so the
catalog still parses.

Usage::

    python -m scripts.pseudoloc --input app/i18n/catalogs/en-SG/main.ftl \\
                                --output app/i18n/catalogs/xx-AC/main.ftl \\
                                --mode AC

    # Batch over all source catalog files:
    python -m scripts.pseudoloc --batch app/i18n/catalogs --source en-SG

    # JSON catalogs (landing/i18n/locales/*):
    python -m scripts.pseudoloc --batch landing/i18n/locales --source en-SG --format json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# Character maps
# --------------------------------------------------------------------------- #

_ACCENT_MAP = {
    "a": "á", "b": "ƀ", "c": "ç", "d": "đ", "e": "é", "f": "ƒ", "g": "ĝ",
    "h": "ĥ", "i": "í", "j": "ĵ", "k": "ķ", "l": "ļ", "m": "ɱ", "n": "ñ",
    "o": "ô", "p": "ƥ", "q": "ɋ", "r": "ŕ", "s": "š", "t": "ţ", "u": "ú",
    "v": "ʋ", "w": "ŵ", "x": "ẋ", "y": "ý", "z": "ž",
    "A": "Á", "B": "Ɓ", "C": "Ç", "D": "Đ", "E": "É", "F": "Ƒ", "G": "Ĝ",
    "H": "Ĥ", "I": "Í", "J": "Ĵ", "K": "Ķ", "L": "Ļ", "M": "Ḿ", "N": "Ñ",
    "O": "Ô", "P": "Ƥ", "Q": "Ǫ", "R": "Ŕ", "S": "Š", "T": "Ţ", "U": "Ú",
    "V": "Ṽ", "W": "Ŵ", "X": "Ẋ", "Y": "Ý", "Z": "Ž",
}

# ICU + Fluent reserved keywords inside selectors — keep ASCII.
_RESERVED_KEYWORDS = {"one", "other", "two", "few", "many", "zero",
                      "male", "female", "true", "false"}

# Matches ICU placeholders we must NOT touch. Brace blocks (which can
# nest arbitrarily for plural/select forms) are handled by an explicit
# scanner — see ``_scan_brace_block`` — because Python's ``re`` cannot
# express arbitrary brace nesting.
#
# This pattern covers everything *except* brace blocks:
#   =0  =1  =42                 — ICU explicit selector keys
#   #                           — ICU current-count token
#   $name                       — Fluent term reference
#   *?[selectorTag]             — Fluent plural/select selector tag, optionally
#                                 prefixed with ``*`` for the default branch.
_PLACEHOLDER_NON_BRACE_RE = re.compile(
    r"""
    (
        \$[A-Za-z_][\w-]*         # Fluent term reference
      | =\d+                      # explicit selector =0 / =1
      | \#                        # ICU '#' = current count
      | \*?\[[A-Za-z_][\w-]*\]    # Fluent selector tag: [one], *[other]
    )
    """,
    re.VERBOSE,
)

# Lines we skip entirely when transforming Fluent (.ftl) files.
_FTL_SKIP_RE = re.compile(r"^\s*(#|$)")  # comment or blank


# --------------------------------------------------------------------------- #
# Core transform
# --------------------------------------------------------------------------- #


def _is_reserved_token(token: str) -> bool:
    """Is this fragment an ICU/Fluent keyword we must keep ASCII?"""
    return token.strip().lower() in _RESERVED_KEYWORDS


def _accent(s: str) -> str:
    return "".join(_ACCENT_MAP.get(c, c) for c in s)


def _expand_padding(s: str, target_ratio: float) -> str:
    """Pad string with filler so visible length ≈ ``target_ratio * len(s)``.

    Padding is appended inside the wrapper, using non-letter glyphs so it's
    obvious it isn't translation content.
    """
    if target_ratio <= 1.0 or not s:
        return s
    extra = max(1, int(len(s) * (target_ratio - 1.0)))
    return s + ("·" * extra)


def _scan_brace_block(s: str, start: int) -> int:
    """Return index just past the matching close brace at ``s[start]`` (``{``).

    Supports nested braces, which ICU plural/select blocks always contain
    (e.g. ``{count, plural, one {1 thing} *[other] {# things}}``). Falls
    back to the position of the next unbalanced close-brace if the input
    is malformed.
    """
    depth = 0
    i = start
    while i < len(s):
        c = s[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(s)


def _split_around_placeholders(s: str) -> list[tuple[str, bool]]:
    """Return list of (chunk, is_placeholder). Placeholders preserved verbatim.

    Recognises:
      * balanced ``{...}`` blocks (with arbitrary nesting — required for
        ICU MessageFormat plural/select forms);
      * Fluent term refs ``$name``;
      * Fluent selector tags ``[one]`` / ``*[other]``;
      * ICU explicit selector keys ``=0`` / ``=1`` …;
      * ICU ``#`` (current-count token).
    """
    parts: list[tuple[str, bool]] = []
    last = 0
    i = 0
    while i < len(s):
        c = s[i]
        if c == "{":
            end = _scan_brace_block(s, i)
            if i > last:
                parts.append((s[last:i], False))
            parts.append((s[i:end], True))
            last = i = end
            continue
        # Try the other (non-brace) placeholder forms via the small regex.
        m = _PLACEHOLDER_NON_BRACE_RE.match(s, i)
        if m:
            if i > last:
                parts.append((s[last:i], False))
            parts.append((m.group(0), True))
            last = i = m.end()
            continue
        i += 1
    if last < len(s):
        parts.append((s[last:], False))
    return parts


def _transform_text_chunk(chunk: str, mode: str) -> str:
    """Apply mode-specific transform to a non-placeholder text fragment.

    Keywords like ``one``/``other`` that sit alone on a fragment are kept
    ASCII so MessageFormat continues to parse.

    Braces ``{``/``}`` that survived the placeholder split (e.g. a stray
    closing brace on a continuation line that closes a block opened on a
    previous line) are preserved verbatim — pseudo-localising them would
    corrupt Fluent structure.
    """
    if _is_reserved_token(chunk):
        return chunk

    if mode == "AC":
        return "".join(_ACCENT_MAP.get(c, c) for c in chunk)
    if mode == "HA":
        return "".join(
            c if (c.isspace() or c in "{}")
            else f"#{c}"
            for c in chunk
        )
    if mode == "LO":
        # Double the visible letters but skip stray braces so structural
        # ``}`` chars aren't doubled into ``}}``.
        doubled = "".join(c if c in "{}" else c for c in chunk)
        # Duplicate only the non-brace portion.
        return doubled + "".join(c for c in chunk if c not in "{}")
    raise ValueError(f"unknown mode {mode!r}")


def pseudo_translate(source: str, mode: str = "AC", *,
                     wrap: bool = True, pad: bool = True) -> str:
    """Transform a single string into its pseudo-locale form.

    Args:
        source: original message (may contain ICU placeholders).
        mode:   ``"AC"``, ``"HA"``, or ``"LO"``.
        wrap:   only meaningful for AC; if False, skip the outer
                ``[ … ]`` wrapper (used for multi-line Fluent values
                where wrapping each line would collide with selector
                tag syntax ``[one]`` / ``*[other]``).
        pad:    if False, suppress the +30 % filler-character padding.
                We disable padding on Fluent lines that open a multi-line
                plural/select block — appending ``·`` after ``->`` would
                make the catalog Junk on re-parse.

    Returns:
        pseudo-localised string with placeholders intact.
    """
    if mode not in {"AC", "HA", "LO"}:
        raise ValueError(f"mode must be AC/HA/LO, got {mode!r}")
    if not source:
        return source

    parts = _split_around_placeholders(source)
    transformed = "".join(
        chunk if is_ph else _transform_text_chunk(chunk, mode)
        for chunk, is_ph in parts
    )

    if mode == "AC":
        if pad:
            # German +30 % length heuristic. Filler is only appended to
            # the visible text, never to placeholders.
            transformed = _expand_padding(transformed, 1.30)
        if wrap:
            transformed = f"[{transformed}]"
    return transformed


def _line_opens_multiline_block(value: str) -> bool:
    """True if ``value`` contains an unclosed ``{`` at end of line.

    Such lines must NOT get trailing padding — the next line is part of
    the same Fluent expression and padding bytes here would corrupt it.

    A line like ``} and { $count ->`` first *closes* a prior block then
    opens a new one. Net depth is 0, but the line still ends with an
    unclosed brace, so we track the *maximum* unclosed depth at any
    point and check the final depth after clamping negatives to 0.
    """
    depth = 0
    for c in value:
        if c == "{":
            depth += 1
        elif c == "}":
            if depth > 0:
                depth -= 1
            # else: this ``}`` closes a block opened on a previous line.
    return depth > 0


def _line_closes_block(value: str) -> bool:
    """True if ``value`` is a structural close like ``}`` or ``*[other]``.

    We avoid padding these — they're Fluent syntax, not message content.
    """
    s = value.strip()
    if not s:
        return True
    if s in ("}", ")"):
        return True
    return False


# --------------------------------------------------------------------------- #
# File-format adapters
# --------------------------------------------------------------------------- #


_FTL_VALUE_RE = re.compile(
    # Fluent IDs are ``[a-zA-Z][a-zA-Z0-9_-]*``; KiX uses dotted keys too
    # (``messages.count``) per landing JSON convention. Match either.
    r"^(?P<lead>\s*(?:-?[a-zA-Z][\w.-]*\s*=|\.[a-zA-Z][\w.-]*\s*=))(?P<val>.*)$"
)


def pseudo_ftl(text: str, mode: str) -> str:
    """Transform a Fluent (.ftl) catalog. Preserves identifiers and comments.

    For multi-line Fluent values, the outer ``[ … ]`` AC wrapper is
    suppressed — Fluent selector tags ``[one]`` / ``*[other]`` would
    collide with it. The accented characters alone give enough visual
    contrast to spot un-localised English.

    Lines that open or close a multi-line plural/select block get no
    +30 % padding either — Fluent treats trailing ``·`` characters as
    syntax violations and would mark the entry as Junk on re-parse.
    """
    out_lines: list[str] = []
    for line in text.splitlines():
        if _FTL_SKIP_RE.match(line):
            out_lines.append(line)
            continue
        m = _FTL_VALUE_RE.match(line)
        if not m:
            # Indented continuation lines and pattern bodies fall through here.
            out_lines.append(_transform_ftl_value_line(line, mode))
            continue
        lead = m.group("lead")
        val = m.group("val")
        pad = not (_line_opens_multiline_block(val) or _line_closes_block(val))
        out_lines.append(lead + pseudo_translate(val, mode, wrap=False, pad=pad))
    return "\n".join(out_lines) + ("\n" if text.endswith("\n") else "")


_FTL_PURE_SYNTAX_RE = re.compile(r"^\s*[}\])]+\s*\.?\s*$")


def _transform_ftl_value_line(line: str, mode: str) -> str:
    """Transform a free-form value line, preserving leading whitespace.

    Pure-syntax lines (e.g. ``    }`` or ``    }.``) are emitted verbatim
    — pseudo-localising the closing brace itself corrupts the Fluent
    structure (``HA`` mode would turn ``}`` into ``#}``).
    """
    if _FTL_PURE_SYNTAX_RE.match(line):
        return line
    stripped_left = line.lstrip(" \t")
    indent = line[: len(line) - len(stripped_left)]
    pad = not (_line_opens_multiline_block(stripped_left)
               or _line_closes_block(stripped_left))
    return indent + pseudo_translate(stripped_left, mode, wrap=False, pad=pad)


def pseudo_json(text: str, mode: str) -> str:
    """Transform a JSON catalog (landing/i18n/locales/<locale>/*.json)."""
    data = json.loads(text)
    out = _walk_json(data, mode)
    return json.dumps(out, ensure_ascii=False, indent=2) + "\n"


def _walk_json(node, mode: str):
    if isinstance(node, str):
        return pseudo_translate(node, mode)
    if isinstance(node, list):
        return [_walk_json(x, mode) for x in node]
    if isinstance(node, dict):
        return {k: _walk_json(v, mode) for k, v in node.items()}
    return node


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _detect_format(path: Path) -> str:
    if path.suffix == ".ftl":
        return "ftl"
    if path.suffix == ".json":
        return "json"
    raise ValueError(f"unknown catalog format for {path}")


def validate_ftl(text: str) -> tuple[int, int]:
    """Validate Fluent text using ``fluent.syntax`` if installed.

    Returns ``(entries, junk_count)``. ``junk_count`` > 0 means the
    transformed catalog has invalid Fluent entries — usually a sign
    that pseudo-localisation corrupted the structure.

    If ``fluent.syntax`` isn't installed we return ``(-1, -1)`` so
    callers can decide whether to skip the check silently.
    """
    try:
        from fluent.syntax import FluentParser  # type: ignore
    except ImportError:
        return (-1, -1)
    parser = FluentParser()
    tree = parser.parse(text)
    junk = sum(1 for e in tree.body if type(e).__name__ == "Junk")
    entries = sum(1 for e in tree.body
                  if type(e).__name__ in ("Message", "Term"))
    return (entries, junk)


def transform_file(src: Path, dst: Path, mode: str,
                   fmt: str | None = None, *, validate: bool = True) -> None:
    fmt = fmt or _detect_format(src)
    text = src.read_text(encoding="utf-8")
    if fmt == "ftl":
        out = pseudo_ftl(text, mode)
    elif fmt == "json":
        out = pseudo_json(text, mode)
    else:
        raise ValueError(f"unsupported format {fmt!r}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(out, encoding="utf-8")
    if validate and fmt == "ftl":
        entries, junk = validate_ftl(out)
        if junk > 0:
            print(f"  warn: {dst}: {junk} Junk entries (entries={entries})",
                  file=sys.stderr)


def _batch(root: Path, source_locale: str, modes: list[str], fmt: str | None) -> int:
    """Generate xx-<MODE>/ catalogs alongside the source locale dir."""
    src_dir = root / source_locale
    if not src_dir.is_dir():
        print(f"source locale dir not found: {src_dir}", file=sys.stderr)
        return 2
    count = 0
    for mode in modes:
        dst_dir = root / f"xx-{mode}"
        for src_file in src_dir.rglob("*"):
            if not src_file.is_file():
                continue
            rel = src_file.relative_to(src_dir)
            dst_file = dst_dir / rel
            try:
                transform_file(src_file, dst_file, mode, fmt)
                count += 1
            except (ValueError, json.JSONDecodeError) as exc:
                print(f"skip {src_file}: {exc}", file=sys.stderr)
    print(f"wrote {count} files under {root}/(xx-AC|xx-HA|xx-LO)")
    return 0


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--input", type=Path,
                   help="single source catalog file (.ftl or .json)")
    p.add_argument("--output", type=Path,
                   help="destination file (only with --input)")
    p.add_argument("--mode", choices=("AC", "HA", "LO"), default="AC",
                   help="pseudo-locale mode")
    p.add_argument("--batch", type=Path,
                   help="batch over a catalog root, e.g. app/i18n/catalogs")
    p.add_argument("--source", default="en-SG",
                   help="source locale subdir when using --batch")
    p.add_argument("--all-modes", action="store_true",
                   help="generate xx-AC, xx-HA and xx-LO when batching")
    p.add_argument("--format", choices=("ftl", "json"), default=None,
                   help="override file-format detection")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    if args.batch:
        modes = ["AC", "HA", "LO"] if args.all_modes else [args.mode]
        return _batch(args.batch, args.source, modes, args.format)

    if not args.input:
        print("--input or --batch required", file=sys.stderr)
        return 2
    if not args.output:
        # Default: write next to source with mode suffix.
        args.output = args.input.with_name(
            f"{args.input.stem}.xx-{args.mode}{args.input.suffix}"
        )
    transform_file(args.input, args.output, args.mode, args.format)
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

"""LLM-powered batch translator for KiX Fluent catalogs.

Turns ``app/i18n/catalogs/en-SG/main.ftl`` into
``app/i18n/catalogs/<target>/main.ftl`` for each target locale, using
Claude Haiku 4.5 as the cheap default. All LLM calls are quota-guarded
via :mod:`scripts.llm_quota_monitor`.

Pipeline (per locale)
=====================
1.  Parse source FTL → list of ``(key, original_text, original_message)``
    tuples. ICU-style placeholders (``{$name}``, plural selectors) are
    *kept structural* — we only translate the surface ``TextElement``
    pieces.
2.  Bucket strings into batches of 20.
3.  For each batch:
       * Check TM cache (`i18n:tm:<hash>:<locale>` in Redis).
       * Build glossary slice → entries whose source_term appears in
         any of the 20 strings.
       * Call Claude (one HTTP call → 20 translations + per-string
         confidence). Cache results.
4.  Serialise translations back into a Fluent AST tree, preserving the
    original Placeable structure → write target ``main.ftl``.

CLI
===
    python -m scripts.i18n_translate --source app/i18n/catalogs/en-SG/main.ftl --target zh-Hans-SG
    python -m scripts.i18n_translate --locales zh-Hans-SG,id-ID,ms-MY
    python -m scripts.i18n_translate --estimate-only --target zh-Hans-SG
    python -m scripts.i18n_translate --review-mode --output review_queue.html --target zh-Hans-SG

Constraints
===========
* Default model: ``claude-haiku-4-5-20251001`` (~$0.001/word).
* Skips real LLM call if ``ANTHROPIC_API_KEY`` is missing — returns a
  deterministic mock translation so the rest of the pipeline can be
  exercised in CI.
* Idempotent: re-running over the same catalog yields the same FTL.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import httpx
from fluent.syntax import ast as fast
from fluent.syntax import parse as fparse
from fluent.syntax import serialize as fserialize

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger("i18n_translate")

CATALOG_DIR = REPO_ROOT / "app" / "i18n" / "catalogs"
DEFAULT_SOURCE = CATALOG_DIR / "en-SG" / "main.ftl"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
BATCH_SIZE = 20
TM_TTL_SECONDS = 30 * 24 * 3600  # 30 days

# ── Locale display-name table for prompts ────────────────────────────────
LOCALE_DISPLAY = {
    "en-SG": "English (Singapore)",
    "en-US": "English (United States)",
    "zh-Hans-SG": "Simplified Chinese (Singapore)",
    "zh-Hans-CN": "Simplified Chinese (China)",
    "zh-Hant-TW": "Traditional Chinese (Taiwan)",
    "zh-Hant-HK": "Traditional Chinese (Hong Kong)",
    "ms-MY": "Malay (Malaysia)",
    "id-ID": "Indonesian (Indonesia)",
    "ta-SG": "Tamil (Singapore)",
    "ta-IN": "Tamil (India)",
    "hi-IN": "Hindi (India)",
    "bn-IN": "Bengali (India)",
    "ja-JP": "Japanese (Japan)",
    "ko-KR": "Korean (South Korea)",
    "th-TH": "Thai (Thailand)",
    "vi-VN": "Vietnamese (Vietnam)",
    "tl-PH": "Filipino (Philippines)",
    "fr-FR": "French (France)",
    "de-DE": "German (Germany)",
    "es-ES": "Spanish (Spain)",
    "pt-BR": "Portuguese (Brazil)",
    "ru-RU": "Russian (Russia)",
    "it-IT": "Italian (Italy)",
    "nl-NL": "Dutch (Netherlands)",
    "pl-PL": "Polish (Poland)",
    "tr-TR": "Turkish (Turkey)",
    "ar-SA": "Arabic (Saudi Arabia)",
}


# ── Quota guard import — must be present, fall through on test envs ──────
def _wait_if_paused_sync(max_wait_seconds: int = 3600) -> bool:
    """Sync façade so non-async callers can still respect the quota flag.

    On any Redis/event-loop failure we fail-open — translation must not
    block when the monitor itself is unavailable (e.g. CI without Redis).
    """
    try:
        from scripts.llm_quota_monitor import wait_if_paused  # type: ignore

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Inside an event loop — caller is responsible for awaiting.
                return False
        except RuntimeError:
            pass
        return asyncio.run(wait_if_paused(max_wait_seconds=max_wait_seconds))
    except Exception as e:  # pragma: no cover — Redis optional
        logger.debug("Quota guard unavailable: %s", e)
        return False


async def _wait_if_paused_async(max_wait_seconds: int = 3600) -> bool:
    try:
        from scripts.llm_quota_monitor import wait_if_paused  # type: ignore

        return await wait_if_paused(max_wait_seconds=max_wait_seconds)
    except Exception as e:  # pragma: no cover — Redis optional
        logger.debug("Quota guard unavailable: %s", e)
        return False


# ── Fluent parsing helpers ───────────────────────────────────────────────


@dataclass
class FluentString:
    """A single translatable unit pulled from a Fluent message.

    The Fluent ``Pattern`` is preserved so we can rebuild the message
    after translation — we only rewrite the ``TextElement`` pieces and
    leave ``Placeable``s (variable refs, plural selectors) untouched.
    """

    key: str                   # "welcome-message" or "welcome-message.description"
    text: str                  # rendered placeholder-aware source text
    pattern: Any               # original fluent.syntax.ast.Pattern
    is_attribute: bool = False


def _pattern_to_translatable(pattern: fast.Pattern) -> str:
    """Render a Fluent Pattern as a string with ICU-style placeholders.

    Variables become ``{$name}``; selectors become a literal MessageFormat
    ``{$var, plural, one {…} *other {…}}`` block so the LLM can preserve
    plural shape without us needing to translate each branch separately.
    """
    out: list[str] = []
    for el in pattern.elements:
        if isinstance(el, fast.TextElement):
            out.append(el.value)
        elif isinstance(el, fast.Placeable):
            out.append(_placeable_to_placeholder(el))
        else:  # pragma: no cover — defensive
            out.append(f"{{<{type(el).__name__}>}}")
    return "".join(out)


def _placeable_to_placeholder(p: fast.Placeable) -> str:
    """Convert a Placeable to a stable ICU-like placeholder string.

    We only translate the surface ``TextElement`` text — Placeables are
    returned verbatim so the LLM does not get to rename ``$name``.
    """
    expr = p.expression
    if isinstance(expr, fast.VariableReference):
        return "{$" + expr.id.name + "}"
    if isinstance(expr, fast.MessageReference):
        return "{" + expr.id.name + "}"
    if isinstance(expr, fast.SelectExpression):
        # Render as `{ $var ->  [k] v *[other] v }` for the LLM, but on
        # write-back we will preserve the original AST instead.
        sel = expr.selector
        sel_name = (
            "$" + sel.id.name if isinstance(sel, fast.VariableReference) else "?"
        )
        variants = []
        for v in expr.variants:
            star = "*" if v.default else ""
            key = (
                v.key.name
                if isinstance(v.key, fast.Identifier)
                else getattr(v.key, "value", "?")
            )
            variants.append(f"{star}[{key}] {_pattern_to_translatable(v.value)}")
        return "{ " + sel_name + " ->\n    " + "\n    ".join(variants) + "\n}"
    if isinstance(expr, fast.StringLiteral):
        return '"' + expr.value + '"'
    return "{...}"


def extract_strings(ftl_text: str) -> list[FluentString]:
    """Parse Fluent source and return the flat list of strings to translate."""
    tree = fparse(ftl_text)
    out: list[FluentString] = []
    for entry in tree.body:
        if isinstance(entry, (fast.Message, fast.Term)):
            if entry.value is not None:
                out.append(
                    FluentString(
                        key=entry.id.name,
                        text=_pattern_to_translatable(entry.value),
                        pattern=entry.value,
                        is_attribute=False,
                    )
                )
            for attr in entry.attributes or []:
                out.append(
                    FluentString(
                        key=f"{entry.id.name}.{attr.id.name}",
                        text=_pattern_to_translatable(attr.value),
                        pattern=attr.value,
                        is_attribute=True,
                    )
                )
    return out


# ── Translation memory ──────────────────────────────────────────────────


def _tm_key(source: str, locale: str) -> str:
    h = hashlib.sha1(source.encode("utf-8")).hexdigest()[:16]
    return f"i18n:tm:{h}:{locale}"


class TMCache:
    """Translation-memory cache. Redis-backed in production; falls back to an
    in-process dict when Redis is not configured.

    Hit-rate counters live under
    ``i18n:tm:stats:{locale}:{hits|misses|writes}`` so the admin
    dashboard endpoint can render TM efficiency.
    """

    def __init__(self) -> None:
        self._memory: dict[str, str] = {}
        self._stats = {"hits": 0, "misses": 0, "writes": 0}
        self._redis = None

    async def _get_redis(self):
        if self._redis is not None:
            return self._redis
        if not os.environ.get("REDIS_URL"):
            return None
        try:
            from redis import asyncio as aioredis  # type: ignore

            self._redis = aioredis.from_url(
                os.environ["REDIS_URL"], decode_responses=True
            )
            return self._redis
        except Exception as e:  # pragma: no cover
            logger.debug("Redis unavailable for TM cache: %s", e)
            return None

    async def get(self, source: str, locale: str) -> str | None:
        k = _tm_key(source, locale)
        r = await self._get_redis()
        if r is not None:
            try:
                v = await r.get(k)
                if v is not None:
                    self._stats["hits"] += 1
                    await r.hincrby(f"i18n:tm:stats:{locale}", "hits", 1)
                    return v
            except Exception as e:  # pragma: no cover
                logger.debug("TM Redis get failed: %s", e)
        v = self._memory.get(k)
        if v is not None:
            self._stats["hits"] += 1
        else:
            self._stats["misses"] += 1
        return v

    async def set(self, source: str, locale: str, translation: str) -> None:
        k = _tm_key(source, locale)
        self._memory[k] = translation
        self._stats["writes"] += 1
        r = await self._get_redis()
        if r is not None:
            try:
                await r.set(k, translation, ex=TM_TTL_SECONDS)
                await r.hincrby(f"i18n:tm:stats:{locale}", "writes", 1)
            except Exception as e:  # pragma: no cover
                logger.debug("TM Redis set failed: %s", e)

    def stats(self) -> dict[str, int]:
        return dict(self._stats)


# ── LLM batch call ──────────────────────────────────────────────────────


@dataclass
class TranslationResult:
    key: str
    source: str
    translation: str
    confidence: str = "medium"   # "high" | "medium" | "low"
    from_cache: bool = False
    glossary_terms: list[str] = field(default_factory=list)


SYSTEM_PROMPT_TEMPLATE = (
    "You are a localization expert. Translate from English (Singapore) to "
    "{target_locale_name}. Preserve ICU MessageFormat syntax (variables in "
    "{{curly braces}}, plural rules). Maintain product terminology per glossary.\n\n"
    "Rules:\n"
    "1. Never translate placeholders like {{$name}}, {{$count}}, {{username}}.\n"
    "2. Preserve plural selector structure: `{{ $count -> [one] X *[other] Y }}` — translate X and Y, keep braces and selectors.\n"
    "3. Keep all glossary do-not-translate terms verbatim.\n"
    "4. Use glossary canonical translations when present.\n"
    "5. Output STRICT JSON. No prose, no markdown fence.\n"
    "Each output object includes: key, translation, confidence (high|medium|low)."
)


async def _llm_translate_batch(
    items: list[FluentString],
    target_locale: str,
    glossary_block: str,
    *,
    model: str,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """One HTTP call → up to ``BATCH_SIZE`` translations.

    Falls back to a deterministic mock if no ``ANTHROPIC_API_KEY``.
    """
    target_name = LOCALE_DISPLAY.get(target_locale, target_locale)
    system = SYSTEM_PROMPT_TEMPLATE.format(target_locale_name=target_name)

    payload_strings = [{"key": it.key, "source": it.text} for it in items]
    user = (
        "Glossary (apply per-string):\n"
        + (glossary_block or "(no glossary terms apply)")
        + "\n\nTranslate each string in the list below. "
        f"Target locale: {target_locale} ({target_name}).\n"
        "Output a JSON array of {key, translation, confidence}.\n\n"
        + json.dumps(payload_strings, ensure_ascii=False)
    )

    await _wait_if_paused_async(max_wait_seconds=3600)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        # Mock-mode policy (Wave B P1): do NOT emit `[locale] English ...`
        # placeholder strings — those leak into the UI as broken copy. We
        # instead echo the English source verbatim (so the rendered UI
        # is at worst English) and tag the result with confidence
        # `needs_translation` so the review queue + sidecar can flag the
        # batch for a real LLM pass once an API key is available.
        logger.warning(
            "ANTHROPIC_API_KEY missing — returning source as TRANSLATION_NEEDED "
            "stubs (locale=%s, n=%d)",
            target_locale, len(items),
        )
        return [
            {
                "key": it.key,
                "translation": it.text,
                "confidence": "needs_translation",
                "needs_translation": True,
            }
            for it in items
        ]

    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                json={
                    "model": model,
                    "max_tokens": 4000,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                },
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            )
        if r.status_code != 200:
            logger.warning("LLM batch HTTP %s — falling back to mock", r.status_code)
            return [
                {"key": it.key, "translation": it.text, "confidence": "low"}
                for it in items
            ]
        body = r.json()
        text = "".join(
            blk.get("text", "")
            for blk in body.get("content", [])
            if blk.get("type") == "text"
        ).strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S).strip()
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "translations" in parsed:
            parsed = parsed["translations"]
        if not isinstance(parsed, list):
            raise ValueError("LLM did not return a list")
        return parsed
    except Exception as e:
        logger.warning("LLM batch failed (%s) — returning mock", e)
        return [
            {"key": it.key, "translation": it.text, "confidence": "low"}
            for it in items
        ]


# ── Write-back ──────────────────────────────────────────────────────────


_PLACEHOLDER_RE = re.compile(r"\{\s*\$([a-zA-Z_][a-zA-Z0-9_-]*)\s*\}")


def _rebuild_pattern(translated: str, original: fast.Pattern) -> fast.Pattern:
    """Rebuild a Fluent Pattern from a translated string.

    Strategy: if the original has only ``TextElement`` + simple variable
    ``Placeable``s, splice variable refs back into the translated text.
    If the original contains a ``SelectExpression`` (plural), keep the
    original pattern verbatim — the LLM is not allowed to restructure
    plural rules in this code path. (Plural translation is a Phase 2
    feature; the model still sees them inside the source so it can
    learn the surrounding tone.)
    """
    has_select = any(
        isinstance(el, fast.Placeable)
        and isinstance(el.expression, fast.SelectExpression)
        for el in original.elements
    )
    if has_select:
        return original

    # Map variable name → original Placeable so we can keep AST identity.
    var_lookup: dict[str, fast.Placeable] = {}
    for el in original.elements:
        if isinstance(el, fast.Placeable) and isinstance(
            el.expression, fast.VariableReference
        ):
            var_lookup[el.expression.id.name] = el

    elements: list[Any] = []
    last = 0
    for m in _PLACEHOLDER_RE.finditer(translated):
        if m.start() > last:
            elements.append(fast.TextElement(translated[last : m.start()]))
        name = m.group(1)
        ph = var_lookup.get(name) or fast.Placeable(
            expression=fast.VariableReference(id=fast.Identifier(name=name))
        )
        elements.append(ph)
        last = m.end()
    if last < len(translated):
        elements.append(fast.TextElement(translated[last:]))
    if not elements:
        elements.append(fast.TextElement(translated))
    return fast.Pattern(elements=elements)


def write_translated_ftl(
    source_ftl: str,
    translations: dict[str, str],
    *,
    locale: str,
) -> str:
    """Build the target FTL text from source + ``{key: translation}`` map.

    Messages absent from ``translations`` are emitted unchanged — that
    way a partial run still produces a syntactically valid file with
    English fallbacks for the missing keys.
    """
    tree = fparse(source_ftl)
    for entry in tree.body:
        if isinstance(entry, (fast.Message, fast.Term)):
            if entry.value is not None and entry.id.name in translations:
                entry.value = _rebuild_pattern(translations[entry.id.name], entry.value)
            for attr in entry.attributes or []:
                k = f"{entry.id.name}.{attr.id.name}"
                if k in translations:
                    attr.value = _rebuild_pattern(translations[k], attr.value)
        elif isinstance(entry, fast.ResourceComment):
            # Update the locale tag in the header comment if present.
            entry.content = re.sub(
                r"English \(Singapore\)", LOCALE_DISPLAY.get(locale, locale), entry.content
            )
    return fserialize(tree)


# ── Orchestration ───────────────────────────────────────────────────────


async def translate_catalog(
    source_path: Path,
    target_locale: str,
    *,
    model: str = DEFAULT_MODEL,
    tm: TMCache | None = None,
    glossary_terms: list[Any] | None = None,
    batch_size: int = BATCH_SIZE,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Translate one catalog into one locale. Returns metadata + results."""
    from scripts.i18n_glossary import (
        format_for_prompt,
        load_glossary,
        terms_appearing_in,
    )

    if tm is None:
        tm = TMCache()
    if glossary_terms is None:
        glossary_terms = load_glossary(target_locale)

    source_text = source_path.read_text(encoding="utf-8")
    strings = extract_strings(source_text)

    results: list[TranslationResult] = []
    batches: list[list[FluentString]] = []
    cache_hits: list[TranslationResult] = []

    pending: list[FluentString] = []
    for s in strings:
        cached = await tm.get(s.text, target_locale)
        if cached is not None:
            cache_hits.append(
                TranslationResult(
                    key=s.key,
                    source=s.text,
                    translation=cached,
                    confidence="high",
                    from_cache=True,
                )
            )
        else:
            pending.append(s)

    for i in range(0, len(pending), batch_size):
        batches.append(pending[i : i + batch_size])

    llm_calls = 0
    for batch in batches:
        joined = "\n".join(s.text for s in batch)
        applicable = terms_appearing_in(joined, glossary_terms)
        gloss_block = format_for_prompt(applicable)
        if dry_run:
            for s in batch:
                results.append(
                    TranslationResult(
                        key=s.key,
                        source=s.text,
                        translation=s.text,
                        confidence="low",
                        glossary_terms=[t.source_term for t in applicable],
                    )
                )
            continue
        raw = await _llm_translate_batch(
            batch, target_locale, gloss_block, model=model
        )
        llm_calls += 1
        by_key = {r.get("key"): r for r in raw if isinstance(r, dict)}
        for s in batch:
            row = by_key.get(s.key) or {}
            tr = str(row.get("translation") or s.text)
            conf = str(row.get("confidence") or "medium").lower()
            if conf not in ("high", "medium", "low", "needs_translation"):
                conf = "medium"
            # Never cache mock-mode stubs — they'd poison the next real LLM run.
            if conf != "needs_translation":
                await tm.set(s.text, target_locale, tr)
            results.append(
                TranslationResult(
                    key=s.key,
                    source=s.text,
                    translation=tr,
                    confidence=conf,
                    glossary_terms=[t.source_term for t in applicable],
                )
            )

    all_results = cache_hits + results
    return {
        "locale": target_locale,
        "source_path": str(source_path),
        "total_strings": len(strings),
        "cache_hits": len(cache_hits),
        "llm_calls": llm_calls,
        "batches": len(batches),
        "results": all_results,
        "source_ftl": source_text,
    }


def persist_catalog(
    bundle: dict[str, Any], catalog_dir: Path | None = None
) -> Path:
    """Write the translated FTL to ``catalogs/<locale>/main.ftl``.

    Also emits a ``_translation_status.json`` sidecar listing every key
    whose confidence is ``needs_translation`` (mock-mode stub) so the
    review queue can prioritise the first real LLM pass.
    """
    out_dir = (catalog_dir or CATALOG_DIR) / bundle["locale"]
    out_dir.mkdir(parents=True, exist_ok=True)
    translations = {r.key: r.translation for r in bundle["results"]}
    ftl = write_translated_ftl(
        bundle["source_ftl"], translations, locale=bundle["locale"]
    )
    out_path = out_dir / "main.ftl"
    out_path.write_text(ftl, encoding="utf-8")
    write_status_sidecar(bundle, out_dir)
    return out_path


def write_status_sidecar(bundle: dict[str, Any], out_dir: Path) -> Path:
    """Emit ``_translation_status.json`` so the review queue can target
    stub-translated keys first.

    Schema::

        {
          "locale": "id-ID",
          "total": 132,
          "needs_translation": 132,
          "auto_translated": 0,
          "reviewed": false,
          "stub_keys": ["welcome-message", ...]
        }
    """
    needs = [
        r.key for r in bundle["results"] if r.confidence == "needs_translation"
    ]
    auto = [
        r.key
        for r in bundle["results"]
        if r.confidence in ("high", "medium", "low")
    ]
    status = {
        "locale": bundle["locale"],
        "total": len(bundle["results"]),
        "needs_translation": len(needs),
        "auto_translated": len(auto),
        "reviewed": False,
        "stub_keys": needs[:200],  # cap for readability
    }
    p = out_dir / "_translation_status.json"
    p.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


# ── Cost estimation ─────────────────────────────────────────────────────

# Claude Haiku 4.5 pricing (rough; used for estimates only).
_INPUT_PRICE_PER_M_TOKENS = 1.00      # $/M input tokens
_OUTPUT_PRICE_PER_M_TOKENS = 5.00     # $/M output tokens
_TOKENS_PER_WORD = 1.4                # English-ish heuristic


def estimate_cost(
    strings: list[FluentString], target_locales: list[str]
) -> dict[str, Any]:
    """Return cost projection for translating ``strings`` × locales.

    Cost model: input tokens = system prompt + 20 strings/batch + glossary
    (~250 tokens overhead per batch); output tokens ~= input
    translation size ×1.5 for CJK expansion.
    """
    total_words = sum(len(re.findall(r"\w+", s.text)) for s in strings)
    n_batches = max(1, (len(strings) + BATCH_SIZE - 1) // BATCH_SIZE)
    overhead_tokens_per_batch = 300  # system + glossary slice
    input_tokens_per_locale = (
        n_batches * overhead_tokens_per_batch
        + total_words * _TOKENS_PER_WORD
    )
    output_tokens_per_locale = total_words * _TOKENS_PER_WORD * 1.5
    cost_per_locale = (
        input_tokens_per_locale / 1_000_000 * _INPUT_PRICE_PER_M_TOKENS
        + output_tokens_per_locale / 1_000_000 * _OUTPUT_PRICE_PER_M_TOKENS
    )
    total_cost = cost_per_locale * len(target_locales)

    # Time estimate: 1 batch ≈ 3s LLM + 1s overhead, sequential.
    seconds_per_locale = n_batches * 4
    total_seconds = seconds_per_locale * len(target_locales)

    return {
        "total_strings": len(strings),
        "total_words": total_words,
        "batches_per_locale": n_batches,
        "input_tokens_per_locale": int(input_tokens_per_locale),
        "output_tokens_per_locale": int(output_tokens_per_locale),
        "cost_per_locale_usd": round(cost_per_locale, 4),
        "target_locales": target_locales,
        "total_cost_usd": round(total_cost, 4),
        "estimated_seconds_per_locale": seconds_per_locale,
        "total_seconds": total_seconds,
        "model": DEFAULT_MODEL,
    }


def render_estimate(est: dict[str, Any]) -> str:
    locales = ", ".join(est["target_locales"])
    return (
        "i18n_translate cost estimate\n"
        "============================\n"
        f"Model:               {est['model']}\n"
        f"Source strings:      {est['total_strings']}\n"
        f"Approx words:        {est['total_words']}\n"
        f"Batches per locale:  {est['batches_per_locale']} (size={BATCH_SIZE})\n"
        f"Input tokens/locale: {est['input_tokens_per_locale']:,}\n"
        f"Output tokens/locale:{est['output_tokens_per_locale']:,}\n"
        f"Cost per locale:     ${est['cost_per_locale_usd']:.4f}\n"
        f"Target locales:      {locales}\n"
        f"Total LLM cost:      ${est['total_cost_usd']:.4f}\n"
        f"Wall time/locale:    ~{est['estimated_seconds_per_locale']}s\n"
        f"Total wall time:     ~{est['total_seconds']}s\n"
    )


# ── Review-mode HTML report ─────────────────────────────────────────────


def render_review_html(bundle: dict[str, Any]) -> str:
    """Render a side-by-side review HTML report for one locale bundle."""
    rows = []
    for r in bundle["results"]:
        gloss = ", ".join(r.glossary_terms) or "&mdash;"
        rows.append(
            f"""<tr class="conf-{r.confidence}">
  <td class="k">{html.escape(r.key)}</td>
  <td class="src">{html.escape(r.source)}</td>
  <td class="tgt">{html.escape(r.translation)}</td>
  <td class="conf">{r.confidence}</td>
  <td class="gloss">{gloss}</td>
  <td class="actions">
    <form method="post" action="/api/v1/admin/translations/mark-reviewed">
      <input type="hidden" name="key" value="{html.escape(r.key)}"/>
      <input type="hidden" name="locale" value="{html.escape(bundle['locale'])}"/>
      <button name="action" value="approve">Approve</button>
      <button name="action" value="reject">Reject</button>
    </form>
  </td>
</tr>"""
        )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>KiX i18n review — {bundle['locale']}</title>
<style>
body {{ font-family: -apple-system, sans-serif; margin: 24px; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ padding: 6px 10px; border-bottom: 1px solid #eee; vertical-align: top; }}
th {{ text-align: left; background: #f4f4f4; position: sticky; top: 0; }}
.k {{ font-family: monospace; color: #555; min-width: 220px; }}
.src, .tgt {{ max-width: 360px; }}
.tgt {{ background: #fbfff5; }}
.conf-high {{ background: #f4fff4; }}
.conf-medium {{ background: #fffbf0; }}
.conf-low {{ background: #fff0f0; }}
.gloss {{ font-size: 11px; color: #888; max-width: 180px; }}
.actions button {{ margin-right: 4px; }}
header {{ margin-bottom: 16px; }}
small {{ color: #888; }}
</style>
</head>
<body>
<header>
  <h1>KiX i18n review — {bundle['locale']}</h1>
  <p>
    {bundle['total_strings']} strings &middot; {bundle['cache_hits']} cache hits
    &middot; {bundle['llm_calls']} LLM calls &middot; {bundle['batches']} batches
  </p>
  <small>Approve/reject form posts to <code>/api/v1/admin/translations/mark-reviewed</code>.</small>
</header>
<table>
  <thead><tr>
    <th>Key</th><th>Source</th><th>Translation</th>
    <th>Confidence</th><th>Glossary</th><th>Action</th>
  </tr></thead>
  <tbody>
    {"".join(rows)}
  </tbody>
</table>
</body></html>"""


# ── DB write-through (Agent 7 integration) ──────────────────────────────


async def mark_auto_translated_in_db(
    bundle: dict[str, Any], session: Any = None
) -> int:
    """Persist each translation to ``brand_translations`` with
    ``auto_translated=true``. Returns rows touched.

    The brand_translations table is keyed by ``(brand_id, field_name,
    locale)``. For UI catalog strings we store ``brand_id="_ui"`` so
    the same review queue surfaces both brand-content and UI strings.

    Silently skipped if Agent 7's service is unavailable or no session
    is supplied — the FTL output is still written.
    """
    try:
        from app.services import brand_translation_service as bts  # type: ignore
    except Exception as e:
        logger.debug("brand_translation_service unavailable: %s", e)
        return 0
    if session is None:
        return 0

    n = 0
    for r in bundle["results"]:
        try:
            await bts.set_translation(
                session,
                brand_id="_ui",
                field=r.key,
                locale=bundle["locale"],
                value=r.translation,
                auto=True,
            )
            n += 1
        except Exception as e:  # pragma: no cover — defensive
            logger.warning("DB upsert failed for %s: %s", r.key, e)
    return n


# ── CLI ────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="KiX i18n batch translator (LLM-powered)")
    p.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help="Source FTL file (defaults to en-SG/main.ftl)",
    )
    p.add_argument(
        "--target",
        help="Single target locale (e.g. zh-Hans-SG)",
    )
    p.add_argument(
        "--locales",
        help="Comma-separated list of target locales (overrides --target)",
    )
    p.add_argument("--model", default=DEFAULT_MODEL, help="Claude model id")
    p.add_argument(
        "--estimate", "--estimate-only", dest="estimate_only", action="store_true",
        help="Print cost projection; do not call the LLM",
    )
    p.add_argument(
        "--review-mode", action="store_true",
        help="Write HTML review queue alongside the FTL output",
    )
    p.add_argument(
        "--output",
        type=Path,
        help="Path for --review-mode HTML output (per locale appended)",
    )
    p.add_argument("--dry-run", action="store_true", help="Skip LLM, mock translations")
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
        print(f"Source FTL not found: {args.source}", file=sys.stderr)
        return 2

    locales = _resolve_locales(args)
    src_text = args.source.read_text(encoding="utf-8")
    strings = extract_strings(src_text)

    if args.estimate_only:
        est = estimate_cost(strings, locales)
        print(render_estimate(est))
        return 0

    tm = TMCache()
    for locale in locales:
        bundle = await translate_catalog(
            args.source,
            locale,
            model=args.model,
            tm=tm,
            dry_run=args.dry_run,
        )
        out_path = persist_catalog(bundle)
        print(
            f"[{locale}] {bundle['total_strings']} strings "
            f"({bundle['cache_hits']} cached, {bundle['llm_calls']} LLM calls) → {out_path}"
        )
        if args.review_mode:
            html_path = (
                args.output if args.output else out_path.with_suffix(".review.html")
            )
            if len(locales) > 1 and args.output:
                # Multiple locales sharing one --output → suffix per locale.
                html_path = args.output.with_name(
                    f"{args.output.stem}.{locale}{args.output.suffix or '.html'}"
                )
            html_path.write_text(render_review_html(bundle), encoding="utf-8")
            print(f"[{locale}] review HTML → {html_path}")

    print("\nTM stats:", tm.stats())
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_cli_async(args))


if __name__ == "__main__":
    sys.exit(main())

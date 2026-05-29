# Adding a new locale

This is the operational checklist for shipping a new locale on the
KiX Platform. The infra was scaffolded in Wave 1 (Project Fluent +
`Accept-Language` middleware); each new locale is a 5-step procedure.

The locale registry, runtime, and middleware live under `app/i18n/`.
The full strategy doc is at `/Users/mozat/a-docs/i18n-trinity-strategy.md`.

## 1. Register the BCP 47 tag

Edit `app/i18n/__init__.py` and append the tag to `SUPPORTED_LOCALES`.

```python
SUPPORTED_LOCALES = [
    "en-SG",
    "zh-Hans-SG",
    "en-US",
    "zh-Hans-CN",
    "id-ID",        # ← new
]
```

If the new locale represents a new base language (e.g. first time we
ship Indonesian), also add an entry to `_LANGUAGE_REGIONAL_FALLBACK`
so cousin locales fall back to a sensible default:

```python
_LANGUAGE_REGIONAL_FALLBACK = {
    "en": "en-US",
    "zh-Hans": "zh-Hans-CN",
    "id": "id-ID",     # ← new
}
```

## 2. Create the catalog directory

Drop a `main.ftl` into `app/i18n/catalogs/<locale>/`. The layout is
fixed — `fluent.runtime`'s loader expects exactly this path.

```
app/i18n/catalogs/
  id-ID/
    main.ftl
```

Seed `main.ftl` by copying `en-US/main.ftl` and translating each
message. Keep message IDs identical across locales — they're the
contract.

## 3. Wire the region (optional)

If the new locale is the primary language of one of the five KiX
regions, edit `app/region.py` and add it to the region's
`language_fallback_chain`. The chain feeds
`LanguageMiddleware._region_default()` when no `Accept-Language`,
`?lang=`, or user pref pins the request.

```python
"id": {
    ...
    "language_fallback_chain": ["id-ID", "en-US"],
    ...
},
```

## 4. Add a smoke test

In `tests/test_i18n_infra.py`, extend
`test_supported_locales_registry_shape` (or write a parallel test) to
assert the new tag is present and resolves at least one message:

```python
def test_id_id_resolves_welcome():
    out = t("welcome-message", locale="id-ID", name="Budi")
    assert "Budi" in out
```

## 5. Run the suite

```sh
pytest tests/test_i18n_infra.py -v
```

Then run the whole suite — existing tests must still pass:

```sh
pytest tests/ -q
```

## Notes

- **Never silently fall back to English.** The runtime emits a
  `i18n.missing_translation` warning whenever a key is absent in
  every catalog in the chain. Monitor this log in production.
- **Plural rules are CLDR-driven.** `fluent.runtime` already ships the
  ruleset — you don't need to hand-craft `{count, plural, ...}`
  variants for languages with no plural morphology (Chinese, Thai,
  Vietnamese, Indonesian). Just use the `*[other]` arm.
- **Do not translate API field names, enum values, or error codes.**
  Only user-visible strings belong in catalogs.
- **Catalog files are diff-friendly.** Use Crowdin's Fluent integration
  or any TMS that speaks ICU MessageFormat for translation workflows.

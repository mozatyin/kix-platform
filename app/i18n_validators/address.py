"""Per-country postal address validation and formatting.

There is no universal address schema. SG uses block/unit/postal; JP
postal-first ordering with prefecture; ID uses kelurahan/kecamatan; BR
hangs on CEP; US lives or dies by ZIP+state. We side-step the
universality trap by pinning every address record to its source
country and dispatching format + validation through a registry.

Public surface
==============

* ``validate_address(country, fields) -> (bool, list[errors])``
* ``format_address(country, fields, style="multiline") -> str``
* ``get_required_fields(country) -> list[str]``
* ``get_field_order(country) -> list[str]``
* ``get_country_address_spec(country) -> dict``  (introspection)

Each per-country spec consists of:

* ``fields`` — ordered list of ``(name, required, regex_or_None)`` triples.
* ``order`` — explicit display order; usually == ``fields`` order but JP
  flips postal_code/prefecture to the top.
* ``postal_code`` — Python regex; ``None`` means no postal code required.
* ``aliases`` — accepted aliases callers may use (e.g. ``zip`` for ``postal_code``).

This module returns *structured* errors so an API layer can echo them
back without sniffing exception strings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.i18n_validators.country import (
    CountryLookupError,
    parse_country_code,
)

__all__ = [
    "AddressValidationError",
    "validate_address",
    "format_address",
    "get_required_fields",
    "get_field_order",
    "get_country_address_spec",
    "list_supported_countries",
]


@dataclass
class AddressValidationError(ValueError):
    """One structured error inside the validate_address list.

    Subclasses ``ValueError`` so it can be raised + caught when used as
    an exception (unsupported-country path), but is *also* used as a
    plain data record inside the ``errors`` list returned by
    :func:`validate_address`.
    """

    error_code: str
    field: str
    message: str

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict:
        return {
            "error_code": self.error_code,
            "field": self.field,
            "message": self.message,
        }


# ── Per-country address specs ────────────────────────────────────────
#
# Each spec is a dict:
#   fields:        list[(name, required, regex_or_None)]
#   order:         list[name]  (display order)
#   postal_label:  human label for the postal-code field
#   country_label: how the country line is displayed at the bottom
#   aliases:       {alias_name: canonical_name}
#
# Where postal regexes come from: standard public-domain postal
# formats published by UPU + country post offices (Royal Mail, USPS,
# Japan Post, IDpos, etc.). Strictly format, not contents.

_SPECS: dict[str, dict] = {
    # ── Singapore ──────────────────────────────────────────────────
    # SG: <block> <street>, #<floor>-<unit>, Singapore <postal>
    "SG": {
        "fields": [
            ("block", True, r"^[A-Za-z0-9\-/]{1,10}$"),
            ("street", True, None),
            ("floor", False, r"^[0-9]{1,3}$"),
            ("unit", False, r"^[0-9A-Za-z\-]{1,8}$"),
            ("building", False, None),
            ("postal_code", True, r"^\d{6}$"),
        ],
        "order": [
            "block", "street", "floor", "unit", "building", "postal_code",
        ],
        "postal_label": "Singapore",
        "country_label": "Singapore",
        "aliases": {"zip": "postal_code", "postcode": "postal_code"},
    },

    # ── China (PRC) ────────────────────────────────────────────────
    # CN postal codes are 6 digits.
    "CN": {
        "fields": [
            ("province", True, None),
            ("city", True, None),
            ("district", False, None),
            ("street", True, None),
            ("building", False, None),
            ("postal_code", True, r"^\d{6}$"),
        ],
        "order": [
            "postal_code", "province", "city", "district", "street",
            "building",
        ],
        "postal_label": "邮编",
        "country_label": "中国",
        "aliases": {"zip": "postal_code", "postcode": "postal_code"},
    },

    # ── United States ──────────────────────────────────────────────
    "US": {
        "fields": [
            ("line1", True, None),
            ("line2", False, None),
            ("city", True, None),
            ("state", True, r"^[A-Z]{2}$"),
            # ZIP or ZIP+4
            ("postal_code", True, r"^\d{5}(-\d{4})?$"),
        ],
        "order": ["line1", "line2", "city", "state", "postal_code"],
        "postal_label": "ZIP",
        "country_label": "United States",
        "aliases": {
            "zip": "postal_code", "zipcode": "postal_code",
            "postcode": "postal_code", "street1": "line1", "street2": "line2",
        },
    },

    # ── United Kingdom ─────────────────────────────────────────────
    # UK postal codes are notoriously varied. Use a sane regex covering
    # the main forms (A9 9AA, A9A 9AA, AA9 9AA, AA9A 9AA, AA99 9AA, A99 9AA).
    "GB": {
        "fields": [
            ("line1", True, None),
            ("line2", False, None),
            ("city", True, None),
            ("county", False, None),
            ("postal_code", True,
             r"^([A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2})$"),
        ],
        "order": ["line1", "line2", "city", "county", "postal_code"],
        "postal_label": "Postcode",
        "country_label": "United Kingdom",
        "aliases": {
            "zip": "postal_code", "postcode": "postal_code",
            "street1": "line1", "street2": "line2",
        },
    },

    # ── Japan ──────────────────────────────────────────────────────
    # JP addresses are written postal-code FIRST. Regex is XXX-XXXX.
    "JP": {
        "fields": [
            ("postal_code", True, r"^\d{3}-?\d{4}$"),
            ("prefecture", True, None),
            ("city", True, None),
            ("ward", False, None),
            ("address_line", True, None),
            ("building", False, None),
        ],
        "order": [
            "postal_code", "prefecture", "city", "ward", "address_line",
            "building",
        ],
        "postal_label": "〒",
        "country_label": "日本",
        "aliases": {"zip": "postal_code", "postcode": "postal_code"},
    },

    # ── Indonesia ──────────────────────────────────────────────────
    "ID": {
        "fields": [
            ("street", True, None),
            ("kelurahan", False, None),
            ("kecamatan", False, None),
            ("city", True, None),
            ("province", True, None),
            ("postal_code", True, r"^\d{5}$"),
        ],
        "order": [
            "street", "kelurahan", "kecamatan", "city", "province",
            "postal_code",
        ],
        "postal_label": "Kode Pos",
        "country_label": "Indonesia",
        "aliases": {"zip": "postal_code", "postcode": "postal_code"},
    },

    # ── Thailand ───────────────────────────────────────────────────
    "TH": {
        "fields": [
            ("line1", True, None),
            ("subdistrict", False, None),
            ("district", True, None),
            ("province", True, None),
            ("postal_code", True, r"^\d{5}$"),
        ],
        "order": [
            "line1", "subdistrict", "district", "province", "postal_code",
        ],
        "postal_label": "ไปรษณีย์",
        "country_label": "Thailand",
        "aliases": {"zip": "postal_code", "postcode": "postal_code"},
    },

    # ── India ──────────────────────────────────────────────────────
    "IN": {
        "fields": [
            ("line1", True, None),
            ("line2", False, None),
            ("city", True, None),
            ("state", True, None),
            ("postal_code", True, r"^\d{6}$"),  # PIN
        ],
        "order": ["line1", "line2", "city", "state", "postal_code"],
        "postal_label": "PIN",
        "country_label": "India",
        "aliases": {
            "pin": "postal_code", "pincode": "postal_code",
            "zip": "postal_code", "postcode": "postal_code",
        },
    },

    # ── Brazil ─────────────────────────────────────────────────────
    # CEP is 8 digits, conventionally formatted XXXXX-XXX.
    "BR": {
        "fields": [
            ("line1", True, None),
            ("line2", False, None),
            ("neighborhood", False, None),
            ("city", True, None),
            ("state", True, r"^[A-Z]{2}$"),
            ("postal_code", True, r"^\d{5}-?\d{3}$"),
        ],
        "order": [
            "line1", "line2", "neighborhood", "city", "state", "postal_code",
        ],
        "postal_label": "CEP",
        "country_label": "Brasil",
        "aliases": {
            "zip": "postal_code", "cep": "postal_code",
            "postcode": "postal_code",
        },
    },

    # ── Australia ──────────────────────────────────────────────────
    "AU": {
        "fields": [
            ("line1", True, None),
            ("line2", False, None),
            ("suburb", True, None),
            ("state", True, r"^(NSW|VIC|QLD|WA|SA|TAS|ACT|NT)$"),
            ("postal_code", True, r"^\d{4}$"),
        ],
        "order": [
            "line1", "line2", "suburb", "state", "postal_code",
        ],
        "postal_label": "Postcode",
        "country_label": "Australia",
        "aliases": {
            "zip": "postal_code", "postcode": "postal_code",
            "city": "suburb",
        },
    },
}


# ── Internal helpers ─────────────────────────────────────────────────


def _resolve_spec(country: str) -> tuple[str, dict]:
    """Resolve any country form to alpha-2 + its spec."""
    code = parse_country_code(country)
    spec = _SPECS.get(code)
    if spec is None:
        raise AddressValidationError(
            error_code="address.unsupported_country",
            field="country",
            message=f"no address spec registered for country {code!r}",
        )
    return code, spec


def _apply_aliases(spec: dict, fields: dict[str, Any]) -> dict[str, Any]:
    """Rewrite caller-supplied aliases to canonical field names."""
    aliases = spec.get("aliases", {})
    out: dict[str, Any] = {}
    for k, v in fields.items():
        canonical = aliases.get(k, k)
        # Caller-supplied None/empty fields are dropped, not preserved as
        # the literal empty string.
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        out[canonical] = v.strip() if isinstance(v, str) else v
    return out


# ── Public API ───────────────────────────────────────────────────────


def get_required_fields(country: str) -> list[str]:
    """Names of required fields for the country (canonical names)."""
    _, spec = _resolve_spec(country)
    return [name for (name, req, _) in spec["fields"] if req]


def get_field_order(country: str) -> list[str]:
    """Display order for the country's fields."""
    _, spec = _resolve_spec(country)
    return list(spec["order"])


def get_country_address_spec(country: str) -> dict:
    """Spec snapshot for introspection / API responses."""
    code, spec = _resolve_spec(country)
    return {
        "country": code,
        "fields": [
            {"name": n, "required": req, "pattern": pat}
            for (n, req, pat) in spec["fields"]
        ],
        "order": list(spec["order"]),
        "postal_label": spec["postal_label"],
        "country_label": spec["country_label"],
        "aliases": dict(spec.get("aliases", {})),
    }


def list_supported_countries() -> list[str]:
    """ISO alpha-2 codes that have an address spec registered."""
    return sorted(_SPECS.keys())


def validate_address(
    country: str, fields: dict[str, Any]
) -> tuple[bool, list[dict]]:
    """Validate a per-country address.

    Returns ``(ok, errors)``. ``errors`` is a list of dicts shaped like
    ``{error_code, field, message}`` — empty when ``ok`` is True.

    Implementation notes:
      * Required-field absence → ``address.missing``.
      * Regex mismatch → ``address.bad_format``.
      * Unsupported country → single error with ``field="country"``.
    """
    errors: list[dict] = []
    try:
        _, spec = _resolve_spec(country)
    except AddressValidationError as exc:
        return False, [exc.to_dict()]
    except CountryLookupError as exc:
        return False, [{
            "error_code": exc.error_code,
            "field": "country",
            "message": exc.message,
        }]

    canonical = _apply_aliases(spec, fields or {})

    for (name, required, pattern) in spec["fields"]:
        val = canonical.get(name)
        if val is None or (isinstance(val, str) and not val.strip()):
            if required:
                errors.append(
                    AddressValidationError(
                        error_code="address.missing",
                        field=name,
                        message=f"{name} is required",
                    ).to_dict()
                )
            continue
        if pattern is not None and isinstance(val, str):
            if not re.match(pattern, val):
                errors.append(
                    AddressValidationError(
                        error_code="address.bad_format",
                        field=name,
                        message=(
                            f"{name} does not match expected format "
                            f"{pattern!r}"
                        ),
                    ).to_dict()
                )

    return (len(errors) == 0), errors


def format_address(
    country: str,
    fields: dict[str, Any],
    style: str = "multiline",
) -> str:
    """Render an address in display form.

    Styles:
      * ``multiline`` — newline-separated lines, country on the last line.
      * ``single``    — comma-separated single-line.
      * ``postal``    — postal-friendly form (country in uppercase last).

    Unknown fields are silently dropped. The address is rendered even
    if validation would fail — formatting and validation are decoupled
    so callers can preview partial inputs.
    """
    code, spec = _resolve_spec(country)
    canonical = _apply_aliases(spec, fields or {})

    # SG canonical line:  Block <block> <street>, #<floor>-<unit>, <bldg>
    if code == "SG":
        parts = []
        head = ""
        if "block" in canonical:
            head = f"Block {canonical['block']} "
        if "street" in canonical:
            head += canonical["street"]
        if head.strip():
            parts.append(head.strip())
        if "floor" in canonical or "unit" in canonical:
            fu = (
                f"#{canonical.get('floor', '')}"
                f"-{canonical.get('unit', '')}"
            ).rstrip("-")
            parts.append(fu)
        if "building" in canonical:
            parts.append(canonical["building"])
        if "postal_code" in canonical:
            parts.append(f"Singapore {canonical['postal_code']}")
        lines = parts
    elif code == "JP":
        # postal first, then prefecture/city/ward, then address_line/building
        lines = []
        if "postal_code" in canonical:
            lines.append(f"〒{canonical['postal_code']}")
        regional = " ".join(
            canonical[k] for k in ("prefecture", "city", "ward")
            if k in canonical
        )
        if regional:
            lines.append(regional)
        if "address_line" in canonical:
            lines.append(canonical["address_line"])
        if "building" in canonical:
            lines.append(canonical["building"])
    elif code == "CN":
        # postal first, then province/city/district/street/building
        lines = []
        head = "".join(
            canonical[k] for k in ("province", "city", "district")
            if k in canonical
        )
        if head:
            lines.append(head)
        if "street" in canonical:
            lines.append(canonical["street"])
        if "building" in canonical:
            lines.append(canonical["building"])
        if "postal_code" in canonical:
            lines.append(canonical["postal_code"])
    else:
        # Generic: walk display order, gather one field per line.
        lines = []
        for name in spec["order"]:
            v = canonical.get(name)
            if not v:
                continue
            if name == "postal_code":
                # Last-line composition handled below per style.
                continue
            lines.append(str(v))

        # Compose city/state/postal final lines for Western-style addresses.
        if code in {"US", "AU", "BR"}:
            tail_bits = []
            for k in ("city", "suburb", "state", "postal_code"):
                if k in canonical:
                    tail_bits.append(str(canonical[k]))
            if tail_bits:
                lines.append(", ".join(tail_bits[:-1]) + " " + tail_bits[-1]
                             if len(tail_bits) > 1 else tail_bits[0])
            # Strip individual city/state we already merged from generic part.
            lines = [
                ln for ln in lines
                if ln not in {
                    canonical.get("city"), canonical.get("state"),
                    canonical.get("suburb"),
                }
            ]
        elif "postal_code" in canonical:
            lines.append(str(canonical["postal_code"]))

    # Country line — appended last.
    country_line = spec["country_label"]
    if style == "postal":
        country_line = country_line.upper()
    lines.append(country_line)

    if style == "single":
        return ", ".join(ln for ln in lines if ln)
    # multiline + postal share newline layout
    return "\n".join(ln for ln in lines if ln)

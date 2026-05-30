#!/usr/bin/env python3
"""KiX Platform — automated security audit scanner.

Static-analysis-only OWASP Top 10 scanner. No live traffic, no DB access,
no penetration testing. Walks ``app/`` and ``scripts/`` and flags patterns
that historically correlate with vulnerabilities.

Usage::

    python scripts/security_audit.py                 # report to stdout
    python scripts/security_audit.py --json out.json # machine-readable
    python scripts/security_audit.py --severity p0   # filter by severity

Exit code is the count of P0 findings (capped at 125) so CI can gate on it::

    python scripts/security_audit.py --severity p0 && echo OK
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

# ── Paths ────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "app"
SCRIPTS_DIR = REPO_ROOT / "scripts"

SCAN_ROOTS = [APP_DIR, SCRIPTS_DIR]

# Files that are intentionally allowed certain patterns (test fixtures, etc.)
ALLOWLIST_PATHS = {
    # The security helper itself describes timing-safe comparison — string
    # match noise.
    "app/security.py",
}


# ── Finding model ────────────────────────────────────────────────────────


@dataclass
class Finding:
    rule_id: str
    owasp: str           # A01..A10
    severity: str        # P0 / P1 / P2
    title: str
    file: str
    line: int
    snippet: str
    remediation: str
    cwe: str = ""
    tags: list[str] = field(default_factory=list)


# ── Rule engine ──────────────────────────────────────────────────────────


def _iter_py_files(roots: Iterable[Path]) -> Iterable[Path]:
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*.py"):
            if "__pycache__" in p.parts or ".pyc" in p.name:
                continue
            yield p


def _read(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


# Each rule: (rule_id, owasp, severity, title, remediation, cwe, pattern, tags)
# ``pattern`` is a compiled regex run against each line.
RULES: list[tuple] = [
    # ── A01 Broken Access Control ────────────────────────────────────────
    (
        "A01-001", "A01:2021", "P0",
        "Money-route may lack auth dependency",
        "Add Depends(get_current_user) and verify token.brand_id == path brand_id "
        "(or operator role). Today /wallet/{brand_id}/* and /campaigns/* mount "
        "without any auth dependency — anyone can topup or charge any brand.",
        "CWE-862",
        re.compile(r'^@router\.(post|put|delete|patch)\("/\{brand_id\}'),
        ["money", "idor"],
    ),
    (
        "A01-002", "A01:2021", "P0",
        "Asset/Resource serve lacks ownership check (IDOR)",
        "Verify the authenticated brand owns the asset before returning a "
        "presigned URL. Today /assets/{asset_id}/serve only checks status, "
        "any caller with a valid asset_id can fetch any brand's content.",
        "CWE-639",
        re.compile(r'router\.get\("/\{asset_id\}/serve"\)'),
        ["idor"],
    ),
    (
        "A01-003", "A01:2021", "P1",
        "brand_id taken from URL/query without JWT cross-check",
        "Compare path/query brand_id to JWT's brand_id claim. tenant_isolation "
        "middleware extracts brand_id from URL but never authorises it.",
        "CWE-639",
        re.compile(r"_extract_brand_id|brand_id\s*=\s*request\.query_params"),
        ["multi-tenant"],
    ),
    # ── A02 Cryptographic Failures ───────────────────────────────────────
    (
        "A02-001", "A02:2021", "P0",
        "Hardcoded default secret / dev token in source",
        "Move to env var with no default; fail closed when unset. Today the "
        "jwt_secret, qr_signing_secret and ADMIN_TOKEN_DEFAULT ship a real "
        "default string in code.",
        "CWE-798",
        re.compile(
            r'(jwt_secret|qr_signing_secret|admin.?token).*=\s*[\'"][^\'"]*(dev|change|stub|admin-dev)',
            re.I,
        ),
        ["secret"],
    ),
    (
        "A02-002", "A02:2021", "P1",
        "Weak hash (md5/sha1) used in security-adjacent path",
        "Use sha256 for everything. md5 is fine for bucketing but never for "
        "auth/integrity. Audit each call site individually.",
        "CWE-327",
        re.compile(r"\bhashlib\.(md5|sha1)\b"),
        ["weak-hash"],
    ),
    (
        "A02-003", "A02:2021", "P1",
        "HS256 JWT signing — symmetric key shared with every service",
        "Move to RS256/EdDSA with a key the verifier can fetch via JWKS. "
        "Symmetric HS256 means any compromised pod can forge tokens.",
        "CWE-326",
        re.compile(r'jwt_algorithm.*=.*[\'"]HS256[\'"]'),
        ["jwt"],
    ),
    # ── A03 Injection ────────────────────────────────────────────────────
    (
        "A03-001", "A03:2021", "P0",
        "SQL via f-string / .format inside text()",
        "Always parameterise with :name binds. Never interpolate user data "
        "into the SQL string.",
        "CWE-89",
        re.compile(r"text\(\s*f[\'\"]|\.execute\(\s*f[\'\"]"),
        ["sqli"],
    ),
    (
        "A03-002", "A03:2021", "P0",
        "Command injection — subprocess shell=True",
        "Use shell=False and a list argv. Never pass user input through shell.",
        "CWE-78",
        re.compile(r"shell\s*=\s*True"),
        ["cmd-injection"],
    ),
    (
        "A03-003", "A03:2021", "P1",
        "Dynamic code execution (eval/exec) on possibly tainted data",
        "Replace eval/exec with explicit parsing (json.loads, ast.literal_eval, "
        "or a domain-specific evaluator).",
        "CWE-94",
        re.compile(r"^[^#]*\b(eval|exec)\s*\("),
        ["rce"],
    ),
    (
        "A03-004", "A03:2021", "P1",
        "pickle.loads — RCE if input untrusted",
        "Use json or msgpack. pickle is RCE-by-design; never unpickle anything "
        "you didn't sign yourself.",
        "CWE-502",
        re.compile(r"pickle\.(loads?|Unpickler)\("),
        ["deserialization"],
    ),
    (
        "A03-005", "A03:2021", "P1",
        "yaml.load without SafeLoader",
        "Use yaml.safe_load. Default yaml.load instantiates arbitrary Python "
        "objects via tag tricks — equivalent to pickle.",
        "CWE-502",
        re.compile(r"yaml\.load\((?!.*Safe)"),
        ["deserialization"],
    ),
    # ── A04 Insecure Design ──────────────────────────────────────────────
    (
        "A04-001", "A04:2021", "P0",
        "Login endpoint lacks rate limit / lockout",
        "Add per-IP and per-account rate limit (e.g. 5 attempts / 15 min then "
        "exponential backoff). Without it /portal/auth/login is brute-forceable.",
        "CWE-307",
        re.compile(r'router\.post\("/login"\)|router\.post\("/token"'),
        ["brute-force"],
    ),
    (
        "A04-002", "A04:2021", "P1",
        "State-changing endpoint without idempotency key",
        "Require Idempotency-Key header on POSTs that move money or grant "
        "rewards. Without it retries duplicate side-effects.",
        "CWE-840",
        re.compile(r'router\.post.*topup|router\.post.*charge|router\.post.*refund'),
        ["idempotency"],
    ),
    # ── A05 Security Misconfiguration ────────────────────────────────────
    (
        "A05-001", "A05:2021", "P1",
        "CORS allow_methods=['*'] + allow_credentials=True",
        "Replace ['*'] with a whitelist (GET/POST/PUT/PATCH/DELETE/OPTIONS) "
        "and restrict allow_headers. The combination of credentials=True and "
        "wildcard methods/headers is risky.",
        "CWE-942",
        re.compile(r'allow_methods\s*=\s*\[\s*[\'"]\*[\'"]'),
        ["cors"],
    ),
    (
        "A05-002", "A05:2021", "P2",
        "env defaults to 'development'",
        "Force env via deploy config; refuse to boot in 'development' if "
        "binding to a non-loopback interface.",
        "CWE-1004",
        re.compile(r'env:\s*str\s*=\s*[\'"]development[\'"]'),
        ["config"],
    ),
    (
        "A05-003", "A05:2021", "P1",
        "Static files mounted without ETag/cache headers",
        "Wrap StaticFiles with cache-control + Strict-Transport-Security + "
        "X-Content-Type-Options headers via response_middleware.",
        "CWE-693",
        re.compile(r"app\.mount\(.*StaticFiles\("),
        ["headers"],
    ),
    # ── A06 Vulnerable Components ────────────────────────────────────────
    (
        "A06-001", "A06:2021", "P1",
        "Floor dep version (>= without upper bound)",
        "Pin a tested upper bound (e.g. ~=, <next-major) and run pip-audit in "
        "CI weekly. Open-ended >= ranges allow silent CVE-inheritance.",
        "CWE-1104",
        re.compile(r'^[a-zA-Z][\w\-\[\]]*>=\d'),  # only applied to requirements.txt
        ["deps"],
    ),
    # ── A07 Auth Failures ────────────────────────────────────────────────
    (
        "A07-001", "A07:2021", "P0",
        "Hardcoded operator credentials in source",
        "Delete the _DEV_OPERATOR_* block. Bootstrap admins via a one-shot "
        "CLI that hashes a randomly generated password and emits it once.",
        "CWE-798",
        re.compile(r"_DEV_OPERATOR_(EMAIL|PASSWORD)"),
        ["secret", "credentials"],
    ),
    (
        "A07-002", "A07:2021", "P1",
        "No 2FA / MFA for portal operators",
        "Add TOTP (pyotp) at /portal/auth/2fa with a recovery-code one-time list. "
        "Mandatory for any role with money-move scope.",
        "CWE-308",
        re.compile(r"_create_portal_access_token|portal_login"),
        ["mfa"],
    ),
    (
        "A07-003", "A07:2021", "P1",
        "JWT not bound to device / session — no jti / no revocation list",
        "Add a jti claim and a Redis-backed denylist checked on every "
        "validate. Today a stolen JWT is valid until exp with no recourse.",
        "CWE-613",
        re.compile(r"jwt\.decode\("),
        ["jwt", "session"],
    ),
    # ── A08 Data Integrity ───────────────────────────────────────────────
    (
        "A08-001", "A08:2021", "P0",
        "Webhook signature secret missing or fallback to empty string",
        "Refuse to boot when STRIPE_WEBHOOK_SECRET (and each PSP secret) is "
        "unset. Today missing secret yields HTTP 503 *per request* but the "
        "service still happily starts.",
        "CWE-345",
        re.compile(r'os\.getenv\([\'"][A-Z_]*WEBHOOK_SECRET[\'"],\s*[\'"][\'"]\)'),
        ["webhook"],
    ),
    # ── A09 Logging Failures ─────────────────────────────────────────────
    (
        "A09-001", "A09:2021", "P1",
        "PII may appear in log lines (email/phone/token/password)",
        "Never log raw email/phone/PAN/token. Use hashed/masked IDs "
        "(e.g. sha256(email)[:12]). Today portal_login logs the raw email.",
        "CWE-532",
        re.compile(
            r"logger\.(info|warning|error|debug)\(.*\b(email|phone|password|token|secret)\b",
            re.I,
        ),
        ["pii"],
    ),
    # ── A10 SSRF ─────────────────────────────────────────────────────────
    (
        "A10-001", "A10:2021", "P0",
        "Outbound HTTP to user-controlled URL — SSRF risk",
        "Validate scheme=https, resolve DNS and refuse RFC1918 / link-local / "
        "metadata IPs, cap redirects, and run via an egress proxy. Today "
        "/assets/upload-from-url follows arbitrary URLs with follow_redirects.",
        "CWE-918",
        re.compile(r"httpx\.AsyncClient\([^)]*follow_redirects\s*=\s*True"),
        ["ssrf"],
    ),
    (
        "A10-002", "A10:2021", "P1",
        "Outbound httpx.get with timeout but no host allowlist",
        "Centralise outbound HTTP through a hardened client that enforces an "
        "allowlist of destination hostnames per integration.",
        "CWE-918",
        re.compile(r"httpx\.(AsyncClient|get|post)\("),
        ["ssrf"],
    ),
]


# Rules that should only be applied to requirements.txt / pyproject.toml.
_PACKAGING_RULES = {"A06-001"}


def scan_file(path: Path) -> list[Finding]:
    if _rel(path) in ALLOWLIST_PATHS:
        return []
    lines = _read(path)
    findings: list[Finding] = []
    for (
        rule_id, owasp, severity, title, remediation, cwe, pattern, tags
    ) in RULES:
        if rule_id in _PACKAGING_RULES:
            continue
        for i, line in enumerate(lines, start=1):
            if pattern.search(line):
                findings.append(
                    Finding(
                        rule_id=rule_id,
                        owasp=owasp,
                        severity=severity,
                        title=title,
                        file=_rel(path),
                        line=i,
                        snippet=line.strip()[:240],
                        remediation=remediation,
                        cwe=cwe,
                        tags=list(tags),
                    )
                )
    return findings


def scan_requirements() -> list[Finding]:
    req = REPO_ROOT / "requirements.txt"
    if not req.exists():
        return []
    findings: list[Finding] = []
    rule = next((r for r in RULES if r[0] == "A06-001"), None)
    if rule is None:
        return []
    rule_id, owasp, severity, title, remediation, cwe, pattern, tags = rule
    for i, line in enumerate(req.read_text().splitlines(), start=1):
        if line.startswith("#") or not line.strip():
            continue
        if pattern.search(line) and "<" not in line and "==" not in line:
            findings.append(
                Finding(
                    rule_id=rule_id,
                    owasp=owasp,
                    severity=severity,
                    title=title,
                    file="requirements.txt",
                    line=i,
                    snippet=line.strip(),
                    remediation=remediation,
                    cwe=cwe,
                    tags=list(tags),
                )
            )
    return findings


# ── Reporters ────────────────────────────────────────────────────────────


SEV_ORDER = {"P0": 0, "P1": 1, "P2": 2}


def sort_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (SEV_ORDER.get(f.severity, 9), f.owasp, f.file, f.line))


def print_human(findings: list[Finding]) -> None:
    findings = sort_findings(findings)
    by_sev: dict[str, int] = {"P0": 0, "P1": 0, "P2": 0}
    for f in findings:
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1

    print("=" * 72)
    print("KiX Platform — Security Audit (static-analysis)")
    print("=" * 72)
    print(f"Total findings : {len(findings)}")
    print(f"  P0 (critical): {by_sev.get('P0', 0)}")
    print(f"  P1 (high)    : {by_sev.get('P1', 0)}")
    print(f"  P2 (medium)  : {by_sev.get('P2', 0)}")
    print()

    for f in findings:
        print(f"[{f.severity}] {f.rule_id} {f.owasp} — {f.title}")
        print(f"  {f.file}:{f.line}")
        print(f"  > {f.snippet}")
        print(f"  fix: {f.remediation}")
        if f.cwe:
            print(f"  cwe: {f.cwe}")
        print()


def print_json(findings: list[Finding], out_path: str) -> None:
    findings = sort_findings(findings)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "total": len(findings),
                "counts": {
                    "P0": sum(1 for f in findings if f.severity == "P0"),
                    "P1": sum(1 for f in findings if f.severity == "P1"),
                    "P2": sum(1 for f in findings if f.severity == "P2"),
                },
                "findings": [asdict(f) for f in findings],
            },
            fh,
            indent=2,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="KiX security static analyser")
    parser.add_argument("--json", dest="json_out", help="Write JSON report to file")
    parser.add_argument(
        "--severity",
        choices=["p0", "p1", "p2"],
        help="Only print findings at or above this severity",
    )
    args = parser.parse_args()

    findings: list[Finding] = []
    for path in _iter_py_files(SCAN_ROOTS):
        findings.extend(scan_file(path))
    findings.extend(scan_requirements())

    if args.severity:
        thresh = SEV_ORDER[args.severity.upper()]
        findings = [f for f in findings if SEV_ORDER.get(f.severity, 9) <= thresh]

    if args.json_out:
        print_json(findings, args.json_out)
    else:
        print_human(findings)

    # Exit code = # of P0 findings, capped so CI can gate on `== 0`.
    p0 = sum(1 for f in findings if f.severity == "P0")
    return min(p0, 125)


if __name__ == "__main__":
    sys.exit(main())

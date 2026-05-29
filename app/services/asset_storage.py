"""Pluggable asset storage backend for the KiX Asset CDN.

Supports:
- LocalStorage  — files under landing/assets (dev/testing, also CI default)
- S3Storage     — AWS S3 production
- OSSStorage    — Aliyun OSS (S3-compatible) for China

Backend selection is driven by env var ``ASSET_STORAGE_BACKEND``
(``local`` | ``s3`` | ``oss``). The asset router never imports a
backend directly; it only depends on the :class:`AssetStorage` ABC
returned by :func:`get_storage`.

Why a service-layer abstraction?
--------------------------------
Production deploys want CDN-fronted S3 (US/EU) or Aliyun OSS (China);
local devs want to round-trip bytes through the same FastAPI process
without provisioning a bucket. The router stays clean by speaking
``put / get / delete / signed_url`` only and lets the backend decide
how to satisfy them.

This module also exposes a stubbed :func:`optimize_image` so the
router can pretend variants exist before Pillow is wired in. The
contract is: input bytes plus a list of variant specs in, dict of
``{variant_name: bytes}`` out — MVP returns ``{"original": data}``.
"""

from __future__ import annotations

import hashlib
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── ABC ───────────────────────────────────────────────────────────────────


class AssetStorage(ABC):
    """Pluggable backend contract for the Asset CDN.

    All methods are async because production backends (boto3 / aioboto3
    / Aliyun OSS SDK) are I/O bound; LocalStorage uses sync filesystem
    calls under the hood but still presents an async signature for a
    consistent caller surface.
    """

    backend_name: str = "abstract"
    public_url_prefix: str = ""

    @abstractmethod
    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str,
        metadata: dict[str, str] | None = None,
    ) -> str:
        """Persist *data* under *key* and return a publicly resolvable URL."""

    @abstractmethod
    async def get(self, key: str) -> bytes:
        """Return raw bytes for *key*."""

    @abstractmethod
    async def delete(self, key: str) -> bool:
        """Hard-delete *key*. Returns True if removed, False if absent."""

    @abstractmethod
    async def signed_url(self, key: str, ttl_seconds: int = 3600) -> str:
        """Return a time-limited URL suitable for private/protected assets."""

    def public_url(self, key: str) -> str:
        """Build the canonical public URL for *key* (CDN-friendly)."""
        prefix = self.public_url_prefix.rstrip("/")
        return f"{prefix}/{key}" if prefix else key


# ── Local backend ─────────────────────────────────────────────────────────


class LocalStorage(AssetStorage):
    """Filesystem backend; assets live under ``landing/assets`` so the
    same path is served by FastAPI's static mount at ``/landing``.

    ``signed_url`` here returns the public URL with a synthetic
    ``token=`` query param — the local backend has nothing to actually
    sign, but the contract is preserved so router code stays uniform.
    """

    backend_name = "local"

    def __init__(self, base_path: Path, public_url_prefix: str = "/landing/assets"):
        self.base_path = Path(base_path)
        self.public_url_prefix = public_url_prefix
        self.base_path.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        # Guard against path traversal — keys must stay inside base_path.
        candidate = (self.base_path / key).resolve()
        base = self.base_path.resolve()
        if not str(candidate).startswith(str(base)):
            raise ValueError(f"key '{key}' escapes asset base path")
        return candidate

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str,
        metadata: dict[str, str] | None = None,
    ) -> str:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        # We also drop a sibling .meta.json so we can introspect what
        # was uploaded without re-parsing the file content.
        if metadata or content_type:
            meta_path = path.with_suffix(path.suffix + ".meta")
            try:
                import json as _json
                meta_path.write_text(
                    _json.dumps(
                        {
                            "content_type": content_type,
                            "metadata": metadata or {},
                        }
                    )
                )
            except OSError:  # pragma: no cover — best effort
                logger.warning("local_storage.meta_write_failed key=%s", key)
        return self.public_url(key)

    async def get(self, key: str) -> bytes:
        path = self._resolve(key)
        if not path.exists():
            raise FileNotFoundError(key)
        return path.read_bytes()

    async def delete(self, key: str) -> bool:
        path = self._resolve(key)
        if not path.exists():
            return False
        try:
            path.unlink()
        except OSError:
            return False
        meta_path = path.with_suffix(path.suffix + ".meta")
        if meta_path.exists():
            try:
                meta_path.unlink()
            except OSError:  # pragma: no cover
                pass
        return True

    async def signed_url(self, key: str, ttl_seconds: int = 3600) -> str:
        # No real signing; we expose a stable deterministic token so
        # tests can assert on URL shape without time-sensitivity.
        token = hashlib.sha256(f"{key}:{ttl_seconds}".encode()).hexdigest()[:16]
        return f"{self.public_url(key)}?token={token}&expires={ttl_seconds}"


# ── S3 / OSS backend ──────────────────────────────────────────────────────


class S3Storage(AssetStorage):
    """AWS S3 (and Aliyun OSS, which is S3-compatible).

    boto3 is intentionally **not** imported at module load — we only
    initialise the client on first use so unit tests and the local
    dev workflow don't have to install AWS SDKs. Until ``boto3`` is
    available the backend operates in *stub mode*: ``put`` returns a
    plausible CDN URL, ``get`` raises NotImplementedError, ``delete``
    returns True. This lets contract tests run before production
    plumbing lands.
    """

    backend_name = "s3"

    def __init__(
        self,
        bucket: str,
        region: str = "us-west-2",
        endpoint_url: str | None = None,
        cdn_domain: str | None = None,
    ):
        self.bucket = bucket
        self.region = region
        self.endpoint_url = endpoint_url
        self.cdn_domain = cdn_domain
        if cdn_domain:
            self.public_url_prefix = f"https://{cdn_domain.rstrip('/')}"
        elif endpoint_url:
            self.public_url_prefix = (
                f"{endpoint_url.rstrip('/')}/{bucket}"
            )
        else:
            self.public_url_prefix = (
                f"https://{bucket}.s3.{region}.amazonaws.com"
            )
        self._client: Any | None = None  # lazy boto3 client

    def _get_client(self) -> Any | None:
        if self._client is not None:
            return self._client
        try:
            import boto3  # type: ignore
        except ImportError:  # pragma: no cover — stub mode
            logger.info("s3_storage.stub_mode (boto3 not installed)")
            return None
        kwargs: dict[str, Any] = {"region_name": self.region}
        if self.endpoint_url:
            kwargs["endpoint_url"] = self.endpoint_url
        self._client = boto3.client("s3", **kwargs)
        return self._client

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str,
        metadata: dict[str, str] | None = None,
    ) -> str:
        client = self._get_client()
        if client is None:
            # Stub: pretend we uploaded.
            logger.info("s3_storage.put_stub bucket=%s key=%s", self.bucket, key)
            return self.public_url(key)
        # Real path: synchronous boto3 call inside async fn — acceptable
        # for MVP; production should swap to aioboto3 / threadpool.
        client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            Metadata=metadata or {},
        )
        return self.public_url(key)

    async def get(self, key: str) -> bytes:
        client = self._get_client()
        if client is None:  # pragma: no cover — stub
            raise NotImplementedError("S3 stub mode: install boto3 to read")
        resp = client.get_object(Bucket=self.bucket, Key=key)
        return resp["Body"].read()

    async def delete(self, key: str) -> bool:
        client = self._get_client()
        if client is None:  # pragma: no cover — stub
            return True
        client.delete_object(Bucket=self.bucket, Key=key)
        return True

    async def signed_url(self, key: str, ttl_seconds: int = 3600) -> str:
        client = self._get_client()
        if client is None:  # pragma: no cover — stub
            return f"{self.public_url(key)}?stub=1&expires={ttl_seconds}"
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=ttl_seconds,
        )


# ── Backend factory ───────────────────────────────────────────────────────


_DEFAULT_LOCAL_PATH = Path("/Users/mozat/kix-platform/landing/assets")


_singleton: AssetStorage | None = None


def get_storage() -> AssetStorage:
    """Return the configured AssetStorage backend (singleton).

    Reads ``ASSET_STORAGE_BACKEND`` once and memoises the resulting
    backend. Tests that need a different backend should call
    :func:`reset_storage` between scenarios.
    """
    global _singleton
    if _singleton is not None:
        return _singleton

    backend = os.environ.get("ASSET_STORAGE_BACKEND", "local").lower()

    if backend == "s3":
        _singleton = S3Storage(
            bucket=os.environ.get("S3_BUCKET", "kix-assets"),
            region=os.environ.get("S3_REGION", "us-west-2"),
            endpoint_url=os.environ.get("S3_ENDPOINT_URL"),
            cdn_domain=os.environ.get("ASSET_CDN_DOMAIN"),
        )
    elif backend == "oss":
        # Aliyun OSS uses S3-compatible signing; endpoint is mandatory.
        _singleton = S3Storage(
            bucket=os.environ.get("OSS_BUCKET", "kix-assets-cn"),
            region=os.environ.get("OSS_REGION", "oss-cn-hangzhou"),
            endpoint_url=os.environ.get(
                "OSS_ENDPOINT", "https://oss-cn-hangzhou.aliyuncs.com"
            ),
            cdn_domain=os.environ.get("ASSET_CDN_DOMAIN"),
        )
    else:
        base = Path(os.environ.get("ASSET_LOCAL_PATH", str(_DEFAULT_LOCAL_PATH)))
        _singleton = LocalStorage(base)

    logger.info("asset_storage.backend=%s", _singleton.backend_name)
    return _singleton


def reset_storage() -> None:
    """Clear the singleton (test-only helper)."""
    global _singleton
    _singleton = None


# ── Optimization (stub) ───────────────────────────────────────────────────


# Maximum size (bytes) accepted per asset type. Enforced at upload time
# by the router. Numbers chosen to match the spec:
#   - logo / icon / thumbnail: small images, 5 MB
#   - hero_image: large banner, 15 MB
#   - video: 50 MB
#   - gif: 5 MB
#   - audio: 10 MB
#   - document: 20 MB (PDF compliance docs can be hefty)
ASSET_SIZE_LIMITS: dict[str, int] = {
    "logo": 5 * 1024 * 1024,
    "hero_image": 15 * 1024 * 1024,
    "thumbnail": 5 * 1024 * 1024,
    "video": 50 * 1024 * 1024,
    "gif": 5 * 1024 * 1024,
    "audio": 10 * 1024 * 1024,
    "document": 20 * 1024 * 1024,
    "icon": 2 * 1024 * 1024,
}

# Default dimensions hint per asset type (used by variant generation
# and by clients that want to know what "right" looks like).
ASSET_TARGET_DIMS: dict[str, tuple[int, int] | None] = {
    "logo": (512, 512),
    "hero_image": (1920, 1080),
    "thumbnail": (300, 300),
    "video": None,
    "gif": None,
    "audio": None,
    "document": None,
    "icon": (64, 64),
}


async def optimize_image(
    data: bytes,
    variants: list[dict[str, Any]],
) -> dict[str, bytes]:
    """Generate variant sizes/formats for image data.

    MVP behaviour
    -------------
    Returns ``{"original": data}`` only. Production should use Pillow
    or libvips to resize + re-encode based on each variant spec, e.g.
    ``{"size": "300x300", "format": "webp", "quality": 82}``.

    The contract is fixed so callers can already wire the endpoint
    today; swapping in real optimisation later is a pure
    implementation change.
    """
    result: dict[str, bytes] = {"original": data}

    # We attempt Pillow if available, but fall through silently — this
    # keeps the router callable in environments without imaging libs.
    try:  # pragma: no cover — only exercised when Pillow installed
        from io import BytesIO

        from PIL import Image  # type: ignore

        img = Image.open(BytesIO(data))
        for spec in variants:
            name = spec.get("name") or _variant_name(spec)
            size_str = spec.get("size")
            fmt = (spec.get("format") or "webp").lower()
            quality = int(spec.get("quality", 82))

            variant_img = img.copy()
            if size_str and "x" in size_str:
                w, h = (int(x) for x in size_str.split("x", 1))
                variant_img.thumbnail((w, h))

            buf = BytesIO()
            save_kwargs: dict[str, Any] = {"format": fmt.upper()}
            if fmt in {"webp", "jpeg", "jpg"}:
                save_kwargs["quality"] = quality
            variant_img.save(buf, **save_kwargs)
            result[name] = buf.getvalue()
    except Exception:
        logger.debug("optimize_image.skip (Pillow unavailable or failed)")

    return result


def _variant_name(spec: dict[str, Any]) -> str:
    size = spec.get("size", "auto")
    fmt = spec.get("format", "orig")
    return f"{size}_{fmt}"


def detect_image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Cheap dimension probe; returns None if we cannot tell.

    Tries Pillow if installed; otherwise sniffs PNG/JPEG/GIF headers
    so we can populate ``dimensions`` for typical uploads without a
    hard dependency on Pillow.
    """
    try:  # pragma: no cover — exercised when Pillow installed
        from io import BytesIO
        from PIL import Image  # type: ignore
        img = Image.open(BytesIO(data))
        return img.size  # (w, h)
    except Exception:
        pass

    # PNG: 8-byte sig, then IHDR chunk at offset 8.
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 24:
        try:
            w = int.from_bytes(data[16:20], "big")
            h = int.from_bytes(data[20:24], "big")
            return (w, h)
        except Exception:
            return None
    # GIF: dims at bytes 6..10 (little-endian)
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) > 10:
        w = int.from_bytes(data[6:8], "little")
        h = int.from_bytes(data[8:10], "little")
        return (w, h)
    # JPEG: scan for SOFx markers (0xFFC0..0xFFCF except 0xC4/0xC8/0xCC)
    if data[:2] == b"\xff\xd8":
        i = 2
        try:
            while i < len(data) - 9:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h = int.from_bytes(data[i + 5 : i + 7], "big")
                    w = int.from_bytes(data[i + 7 : i + 9], "big")
                    return (w, h)
                seg_len = int.from_bytes(data[i + 2 : i + 4], "big")
                i += 2 + seg_len
        except Exception:
            return None
    return None


__all__ = [
    "AssetStorage",
    "LocalStorage",
    "S3Storage",
    "get_storage",
    "reset_storage",
    "optimize_image",
    "detect_image_dimensions",
    "ASSET_SIZE_LIMITS",
    "ASSET_TARGET_DIMS",
]

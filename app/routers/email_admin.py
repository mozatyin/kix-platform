"""Admin endpoints for the email + push template registry.

All endpoints require the standard ``KIX_ADMIN_TOKEN`` (same gate as
``app/routers/campaigns.py::_check_admin``). Endpoints are read-only
or test-only — they cannot mutate template content (templates live in
code, not Redis). To change a template, ship code.

Routes
------

``GET  /api/v1/admin/email-templates``
    List every template id with category + supported locales.

``GET  /api/v1/admin/email-templates/{tid}/preview``
    Render the template with query-string vars. Returns the same
    shape as ``render_email``. Helpful for QA + design review.

``POST /api/v1/admin/email-templates/{tid}/send-test``
    Render the template and RPUSH it onto a designated test queue.
    The email worker drains that queue and (in prod) ships it to
    SES — in tests it's a no-op + log.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.email_templates import (
    EMAIL_TEMPLATES,
    PUSH_TEMPLATES,
    EmailTemplate,
    PushTemplate,
)
from app.redis_client import get_redis
from app.security import constant_time_eq
from app.services.email_template_service import (
    email_queue_key,
    enqueue_email,
    enqueue_push,
    render_email,
)

logger = logging.getLogger(__name__)
router = APIRouter()

ADMIN_TOKEN_DEFAULT = "admin-dev-token"


def _check_admin(token: str | None) -> None:
    """Mirror of ``campaigns._check_admin`` — kept inline so this
    router has no dependency on the campaigns module."""
    if not token:
        raise HTTPException(status_code=403, detail="admin token required")
    expected = os.getenv("KIX_ADMIN_TOKEN", ADMIN_TOKEN_DEFAULT)
    if not constant_time_eq(token, expected):
        raise HTTPException(status_code=403, detail="invalid admin token")


def _admin_token_from_request(request: Request) -> str | None:
    """Accept the token in either ``?admin_token=`` or ``X-Admin-Token``
    header — matches the convention used by other admin endpoints in
    the platform."""
    qs = request.query_params.get("admin_token")
    if qs:
        return qs
    return request.headers.get("x-admin-token")


# ── List ──────────────────────────────────────────────────────────────────


@router.get("/email-templates")
async def list_email_templates(request: Request) -> dict[str, Any]:
    _check_admin(_admin_token_from_request(request))
    items: list[dict[str, Any]] = []
    for tid, t in sorted(EMAIL_TEMPLATES.items()):
        items.append({
            "template_id": tid,
            "category": t.category,
            "locales_supported": list(t.locales_supported),
            "required_vars": list(t.required_vars),
            "kind": "email",
        })
    for tid, p in sorted(PUSH_TEMPLATES.items()):
        items.append({
            "template_id": tid,
            "category": p.category,
            "locales_supported": list(p.locales_supported),
            "required_vars": list(p.required_vars),
            "kind": "push",
        })
    return {"templates": items, "count": len(items)}


# ── Preview ───────────────────────────────────────────────────────────────


@router.get("/email-templates/{template_id}/preview")
async def preview_email_template(
    template_id: str,
    request: Request,
    locale: str = Query("en-SG"),
) -> dict[str, Any]:
    _check_admin(_admin_token_from_request(request))
    if template_id not in EMAIL_TEMPLATES and template_id not in PUSH_TEMPLATES:
        raise HTTPException(status_code=404, detail="template not found")

    # Pull arbitrary variable values from the rest of the query string.
    reserved = {"admin_token", "locale"}
    template_vars: dict[str, Any] = {
        k: v for k, v in request.query_params.items() if k not in reserved
    }
    try:
        rendered = render_email(template_id, locale, **template_vars)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"template_id": template_id, "locale": locale, "rendered": rendered}


# ── Send-test ─────────────────────────────────────────────────────────────


class SendTestRequest(BaseModel):
    admin_token: str = Field(..., min_length=1, max_length=512)
    brand_id: str = Field(default="test")
    locale: str = Field(default="en-SG")
    recipient: str | None = Field(default=None)
    template_vars: dict[str, Any] = Field(default_factory=dict)


@router.post("/email-templates/{template_id}/send-test")
async def send_test_email_template(
    template_id: str,
    body: SendTestRequest,
    r: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    _check_admin(body.admin_token)
    if template_id in EMAIL_TEMPLATES:
        try:
            envelope = await enqueue_email(
                r,
                brand_id=body.brand_id,
                template_id=template_id,
                locale=body.locale,
                recipient=body.recipient,
                **body.template_vars,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        queue = email_queue_key(body.brand_id)
        return {
            "enqueued": True,
            "queue": queue,
            "kind": "email",
            "envelope": envelope,
        }
    if template_id in PUSH_TEMPLATES:
        try:
            envelope = await enqueue_push(
                r,
                brand_id=body.brand_id,
                template_id=template_id,
                locale=body.locale,
                recipient_kid=body.recipient,
                **body.template_vars,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "enqueued": True,
            "queue": f"push_queue:brand:{body.brand_id}",
            "kind": "push",
            "envelope": envelope,
        }
    raise HTTPException(status_code=404, detail="template not found")


# Backwards-discovery helper for callers that want the EmailTemplate
# / PushTemplate descriptor object itself (not exposed over HTTP).
def _template_descriptor(template_id: str) -> EmailTemplate | PushTemplate:
    if template_id in EMAIL_TEMPLATES:
        return EMAIL_TEMPLATES[template_id]
    if template_id in PUSH_TEMPLATES:
        return PUSH_TEMPLATES[template_id]
    raise HTTPException(status_code=404, detail="template not found")


# Defensive: in some deployments the status import is unused. Keep
# it referenced so `from fastapi import status` linters stay happy.
_ = status

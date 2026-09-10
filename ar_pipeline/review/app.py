"""FastAPI router for the review UI (mounted at ``/review`` by ``main.py``)."""

from __future__ import annotations

import pathlib
import re
import uuid
from collections.abc import Iterator
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ar_pipeline.config import get_settings
from ar_pipeline.db.base import get_session
from ar_pipeline.pipeline.routing import AUTO_REVIEWER
from ar_pipeline.review.auth import (
    COOKIE_NAME,
    User,
    current_user,
    get_auth_provider,
    require_user,
)

TEMPLATES_DIR = pathlib.Path(__file__).parent / "templates"
STATIC_DIR = pathlib.Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(prefix="/review")

_COOKIE_MAX_AGE = 7 * 24 * 60 * 60

_SAFE_INLINE_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
}


def get_db() -> Iterator[Session]:
    with get_session() as session:
        yield session


def _render(request: Request, name: str, /, **ctx: object) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> Response:
    if current_user(request) is not None:
        return RedirectResponse("/review", status_code=303)
    return _render(request, "login.html", error=None)


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    password: str = Form(default=""),
    name: str = Form(default=""),
) -> Response:
    provider = get_auth_provider()
    if name.strip() == "":
        return _render(request, "login.html", error="Enter your name.")
    if name.strip().lower() == AUTO_REVIEWER:
        return _render(request, "login.html", error='"auto" is a reserved name — pick another.')
    if not provider.check_password(password):
        return _render(request, "login.html", error="That password is incorrect.")
    token = provider.issue_session(name.strip())
    resp = RedirectResponse("/review", status_code=303)
    resp.set_cookie(
        COOKIE_NAME,
        token,
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=get_settings().review_cookie_secure,
    )
    return resp


@router.post("/logout")
def logout() -> Response:
    resp = RedirectResponse("/review/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp


@router.get("", response_class=HTMLResponse)
def journey_page(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_db),
) -> Response:
    from ar_pipeline.review.service import list_journey

    return _render(
        request,
        "journey.html",
        user=user,
        rows=list_journey(session),
        flash=request.query_params.get("flash"),
    )


@router.get("/queue", response_class=HTMLResponse)
def queue_page(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_db),
) -> Response:
    from ar_pipeline.review.service import list_pending

    return _render(
        request,
        "queue.html",
        user=user,
        rows=list_pending(session),
        flash=request.query_params.get("flash"),
    )


@router.get("/errors", response_class=HTMLResponse)
def errors_page(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_db),
) -> Response:
    from ar_pipeline.review.service import list_errored, list_failed_deliveries

    return _render(
        request,
        "errors.html",
        user=user,
        emails=list_errored(session),
        failed_deliveries=list_failed_deliveries(session),
        flash=request.query_params.get("flash"),
    )


@router.post("/errors/{email_id}/retry")
def retry_action(
    email_id: uuid.UUID,
    user: User = Depends(require_user),
    session: Session = Depends(get_db),
) -> Response:
    from ar_pipeline.review.service import ReviewError, retry_email

    try:
        retry_email(session, email_id)
    except ReviewError as exc:
        return RedirectResponse(f"/review/errors?flash={quote(str(exc))}", status_code=303)
    return RedirectResponse("/review/errors?flash=Retry+queued", status_code=303)


@router.post("/deliveries/{delivery_id}/resend")
def resend_delivery_action(
    delivery_id: uuid.UUID,
    user: User = Depends(require_user),
    session: Session = Depends(get_db),
) -> Response:
    from ar_pipeline.review.service import ReviewError, resend_delivery

    try:
        resend_delivery(session, delivery_id)
    except ReviewError as exc:
        return RedirectResponse(f"/review/errors?flash={quote(str(exc))}", status_code=303)
    return RedirectResponse("/review/errors?flash=Resend+queued", status_code=303)


@router.get("/extraction/{extraction_id}", response_class=HTMLResponse)
def extraction_view_page(
    request: Request,
    extraction_id: uuid.UUID,
    user: User = Depends(require_user),
    session: Session = Depends(get_db),
) -> Response:
    from ar_pipeline.review.service import ReviewError, load_extraction_view

    try:
        view = load_extraction_view(session, extraction_id)
    except ReviewError:
        raise HTTPException(status_code=404, detail="not found") from None

    if request.query_params.get("format") == "json":
        return Response(
            view.canonical_json,
            media_type="application/json",
            headers={"X-Content-Type-Options": "nosniff"},
        )
    return _render(request, "extraction.html", user=user, view=view)


@router.post("/{extraction_id}/edit")
async def edit_action(
    request: Request,
    extraction_id: uuid.UUID,
    user: User = Depends(require_user),
    session: Session = Depends(get_db),
) -> Response:
    from ar_pipeline.review.forms import parse_form_to_canonical
    from ar_pipeline.review.service import ReviewError, save_edits

    raw = await request.form()
    approve = raw.get("approve") == "1"
    canonical = parse_form_to_canonical({k: v for k, v in raw.items() if isinstance(v, str)})
    try:
        save_edits(session, extraction_id, user, canonical, approve=approve)
    except ReviewError as exc:
        return RedirectResponse(f"/review/{extraction_id}?flash={quote(str(exc))}", status_code=303)
    if approve:
        return RedirectResponse("/review?flash=Approved", status_code=303)
    return RedirectResponse(f"/review/{extraction_id}?flash=Saved", status_code=303)


@router.post("/{extraction_id}/reject")
def reject_action(
    extraction_id: uuid.UUID,
    reason: str = Form(default=""),
    user: User = Depends(require_user),
    session: Session = Depends(get_db),
) -> Response:
    from ar_pipeline.review.service import ReviewError, reject_extraction

    try:
        reject_extraction(session, extraction_id, user, reason)
    except ReviewError as exc:
        return RedirectResponse(f"/review/{extraction_id}?flash={quote(str(exc))}", status_code=303)
    return RedirectResponse("/review?flash=Rejected", status_code=303)


@router.post("/{extraction_id}/reprocess")
def reprocess_action(
    extraction_id: uuid.UUID,
    user: User = Depends(require_user),
    session: Session = Depends(get_db),
) -> Response:
    from ar_pipeline.review.service import ReviewError, load_detail, reprocess_email

    try:
        view = load_detail(session, extraction_id)
        reprocess_email(session, view.email.id)
    except ReviewError as exc:
        return RedirectResponse(f"/review/{extraction_id}?flash={quote(str(exc))}", status_code=303)
    return RedirectResponse("/review?flash=Reprocessing", status_code=303)


@router.get("/{extraction_id}", response_class=HTMLResponse)
def detail_page(
    request: Request,
    extraction_id: uuid.UUID,
    user: User = Depends(require_user),
    session: Session = Depends(get_db),
) -> Response:
    import nh3

    from ar_pipeline.review.service import ReviewError, load_detail

    try:
        view = load_detail(session, extraction_id)
    except ReviewError:
        raise HTTPException(status_code=404, detail="not found") from None

    body_html = nh3.clean(view.email.body_html) if view.email.body_html else ""
    return _render(
        request,
        "detail.html",
        user=user,
        view=view,
        canonical=view.extraction.canonical or {},
        safe_body_html=body_html,
        flash=request.query_params.get("flash"),
    )


@router.get("/{extraction_id}/attachment/{attachment_id}")
def attachment_stream(
    extraction_id: uuid.UUID,
    attachment_id: uuid.UUID,
    user: User = Depends(require_user),
    session: Session = Depends(get_db),
) -> Response:
    from ar_pipeline.db.models import Attachment, Extraction
    from ar_pipeline.storage import attachment_blob_key, get_blob_store

    ext = session.get(Extraction, extraction_id)
    att = session.get(Attachment, attachment_id)
    if ext is None or att is None or att.email_id != ext.email_id:
        raise HTTPException(status_code=404, detail="not found")
    data = get_blob_store().get(attachment_blob_key(att))
    ct = (att.content_type or "").split(";")[0].strip().lower()
    inline = ct in _SAFE_INLINE_TYPES
    safe_name = re.sub(r"[^A-Za-z0-9._ -]", "_", att.filename or "attachment") or "attachment"
    disposition = "inline" if inline else "attachment"
    return Response(
        content=data,
        media_type=ct if inline else "application/octet-stream",
        headers={
            "Content-Disposition": f'{disposition}; filename="{safe_name}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )

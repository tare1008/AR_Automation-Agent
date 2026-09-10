"""FastAPI router for the review UI (mounted at ``/review`` by ``main.py``)."""

from __future__ import annotations

import pathlib
from collections.abc import Iterator

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ar_pipeline.db.base import get_session
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
    if not provider.check_password(password):
        return _render(request, "login.html", error="That password is incorrect.")
    token = provider.issue_session(name.strip())
    resp = RedirectResponse("/review", status_code=303)
    resp.set_cookie(COOKIE_NAME, token, max_age=_COOKIE_MAX_AGE, httponly=True, samesite="lax")
    return resp


@router.post("/logout")
def logout() -> Response:
    resp = RedirectResponse("/review/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp


@router.get("", response_class=HTMLResponse)
def queue_page(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_db),
) -> Response:
    # queue rows are filled in Task 6; empty list keeps the redirect target valid.
    return _render(
        request,
        "queue.html",
        user=user,
        rows=[],
        flash=request.query_params.get("flash"),
    )

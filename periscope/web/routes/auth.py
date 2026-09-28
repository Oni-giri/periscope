"""Setup, login, and logout routes for the web UI."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from periscope.web import auth as web_auth

router = APIRouter()


def _data_dir(request: Request):
    return request.app.state.config.data_dir


def _is_https(request: Request) -> bool:
    return web_auth.request_is_https(request.scope)


def _set_session_cookie(response: Response, token: str, *, secure: bool) -> None:
    response.headers.append(
        "Set-Cookie",
        web_auth.cookie_header_value(token, secure=secure),
    )


def _clear_session_cookie(response: Response, *, secure: bool) -> None:
    response.headers.append(
        "Set-Cookie",
        web_auth.cookie_header_value("", secure=secure, clear=True),
    )


def _render(
    request: Request,
    *,
    name: str,
    title: str,
    error: str | None = None,
    next_path: str = "/",
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request=request,
        name=name,
        context={
            "request": request,
            "title": title,
            "error": error,
            "next_path": next_path,
            "password_set": web_auth.password_is_set(_data_dir(request)),
        },
    )


@router.get("/auth/setup", response_class=HTMLResponse, name="auth_setup")
async def setup_get(request: Request) -> Response:
    if web_auth.password_is_set(_data_dir(request)):
        return RedirectResponse(url="/auth/login", status_code=303)
    return _render(request, name="auth_setup.html", title="Create password")


@router.post("/auth/setup", response_class=HTMLResponse)
async def setup_post(request: Request) -> Response:
    if web_auth.password_is_set(_data_dir(request)):
        return RedirectResponse(url="/auth/login", status_code=303)
    form = await request.form()
    password = str(form.get("password", ""))
    confirm = str(form.get("confirm", ""))
    if len(password) < 8:
        return _render(
            request,
            name="auth_setup.html",
            title="Create password",
            error="Password must be at least 8 characters.",
        )
    if password != confirm:
        return _render(
            request,
            name="auth_setup.html",
            title="Create password",
            error="Passwords do not match.",
        )
    try:
        record = web_auth.create_password(_data_dir(request), password)
    except FileExistsError:
        return RedirectResponse(url="/auth/login", status_code=303)
    token = web_auth.issue_session_token(record.session_secret)
    response = RedirectResponse(url="/", status_code=303)
    _set_session_cookie(response, token, secure=_is_https(request))
    return response


@router.get("/auth/login", response_class=HTMLResponse, name="auth_login")
async def login_get(request: Request, next: str = "/") -> Response:
    if not web_auth.password_is_set(_data_dir(request)):
        return RedirectResponse(url="/auth/setup", status_code=303)
    next_path = web_auth.safe_next_path(next, default="/")
    return _render(
        request,
        name="auth_login.html",
        title="Log in",
        next_path=next_path,
    )


@router.post("/auth/login", response_class=HTMLResponse)
async def login_post(request: Request) -> Response:
    if not web_auth.password_is_set(_data_dir(request)):
        return RedirectResponse(url="/auth/setup", status_code=303)
    form = await request.form()
    password = str(form.get("password", ""))
    next_path = web_auth.safe_next_path(str(form.get("next", "/")), default="/")
    record = web_auth.load_web_auth(_data_dir(request))
    assert record is not None
    if not web_auth.verify_password(password, record.password_hash):
        return _render(
            request,
            name="auth_login.html",
            title="Log in",
            error="Incorrect password.",
            next_path=next_path,
        )
    token = web_auth.issue_session_token(record.session_secret)
    response = RedirectResponse(url=next_path, status_code=303)
    _set_session_cookie(response, token, secure=_is_https(request))
    return response


@router.post("/auth/logout", name="auth_logout")
async def logout_post(request: Request) -> Response:
    response = RedirectResponse(url="/auth/login", status_code=303)
    _clear_session_cookie(response, secure=_is_https(request))
    return response

"""Server-rendered admin console routes (mounted at /admin)."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import auth as admin_auth
from app.admin.auth import AdminSession, check_csrf, issue_csrf, require_session
from app.admin.reads import duplicate_pairs, get_history, list_projects, stale_entries
from app.admin.render import render_markdown
from app.auth import hash_api_key, lookup_auth_context
from app.config import get_settings
from app.db import get_session
from app.services.memory import ServiceError, compact_entry, delete_entry, get_entries, list_entries
from app.services.search_entry import run_search

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _csrf_context(request: Request) -> dict[str, str]:
    """Expose the session CSRF token to every authenticated template render.

    ``base.html`` renders the logout form on all signed-in pages, so the token
    must be available without each route threading it through explicitly. Only
    minted once a session exists, so anonymous pages (login) get no cookie churn.
    """
    if request.session.get("admin"):
        return {"csrf": issue_csrf(request)}
    return {}


templates = Jinja2Templates(directory=str(_TEMPLATES_DIR), context_processors=[_csrf_context])

admin_router = APIRouter(prefix="/admin")


@admin_router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"error": None})


@admin_router.post("/login", response_model=None)
async def login_submit(
    request: Request,
    api_key: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse | RedirectResponse:
    ip = request.client.host if request.client else "unknown"
    if admin_auth.login_throttled(ip):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Too many attempts, try again shortly."},
            status_code=429,
        )
    ctx = await lookup_auth_context(session, hash_api_key(api_key.strip()))
    if ctx is None:
        admin_auth.record_failed_login(ip)
        return templates.TemplateResponse(
            request, "login.html", {"error": "Invalid API key."}, status_code=200
        )
    admin_auth.clear_login_failures(ip)
    admin_auth.set_session(request, ctx.key_id, ctx.owner_name, ctx.team_id, ctx.team_name)
    return RedirectResponse("/admin", status_code=303)


@admin_router.post("/logout", response_model=None)
async def logout(
    request: Request,
    csrf: str | None = Form(default=None),
    admin: AdminSession = Depends(require_session),
) -> PlainTextResponse | RedirectResponse:
    # State-changing → CSRF-guarded POST, same contract as delete/compact.
    if not check_csrf(request, csrf):
        return PlainTextResponse("bad csrf", status_code=400)
    admin_auth.clear_session(request)
    return RedirectResponse("/admin/login", status_code=303)


@admin_router.get("", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    admin: AdminSession = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    projects = await list_projects(session, admin.team_id)
    return templates.TemplateResponse(
        request, "dashboard.html", {"admin": admin, "projects": projects}
    )


@admin_router.get("/projects/{slug}", response_class=HTMLResponse)
async def project_page(
    request: Request,
    slug: str,
    msg: str | None = None,
    admin: AdminSession = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    listing = await list_entries(session, admin.team_id, slug)
    return templates.TemplateResponse(
        request,
        "project.html",
        {
            "admin": admin,
            "slug": slug,
            "entries": listing["entries"],
            "msg": msg,
        },
    )


@admin_router.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request,
    q: str | None = None,
    project: str | None = None,
    scope: str = "project",
    limit: int = 5,
    admin: AdminSession = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    if scope not in ("project", "all"):
        scope = "project"
    payload: dict | None = None
    vector_rescued = False
    msg: str | None = None
    if q and scope == "project" and not (project or "").strip():
        # Reachable global entry point (e.g. /admin/search?q=foo with no project) —
        # normalize_keyword("") would raise; render an empty state instead of 500.
        msg = 'Pick a project, or switch scope to "all", to search.'
    elif q:
        # scope="all" ignores project; scope="project" needs one.
        target = project or ""
        raw = await run_search(session, admin.team_id, target, q, limit=limit, scope=scope)
        vector_rescued = bool(raw.pop("vector_rescued", False))
        payload = raw  # exact agent-facing payload
    search_mode = "hybrid" if get_settings().enable_hybrid_search else "lexical"
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "admin": admin,
            "q": q,
            "project": project,
            "scope": scope,
            "limit": limit,
            "msg": msg,
            "payload": payload,
            "payload_json": json.dumps(payload, indent=2, default=str)
            if payload is not None
            else "",
            "search_mode": search_mode,
            "vector_rescued": vector_rescued,
        },
    )


@admin_router.get("/memory/{keyword}", response_class=HTMLResponse)
async def memory_page(
    request: Request,
    keyword: str,
    project: str,
    msg: str | None = None,
    admin: AdminSession = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    got = await get_entries(session, admin.team_id, project, [keyword])
    if not got["entries"]:
        raise HTTPException(status_code=404, detail="not found")
    entry = got["entries"][0]
    history = await get_history(session, admin.team_id, project, keyword)
    return templates.TemplateResponse(
        request,
        "memory.html",
        {
            "admin": admin,
            "slug": project,
            "entry": entry,
            "history": history,
            "csrf": issue_csrf(request),
            "msg": msg,
            "content_html": render_markdown(entry["content"]),
            "fragments_html": [render_markdown(f["content"]) for f in entry["fragments"]],
        },
    )


@admin_router.post("/memory/{keyword}/delete", response_model=None)
async def memory_delete(
    request: Request,
    keyword: str,
    project: str,
    csrf: str | None = Form(default=None),
    admin: AdminSession = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> PlainTextResponse | RedirectResponse:
    if not check_csrf(request, csrf):
        return PlainTextResponse("bad csrf", status_code=400)
    try:
        await delete_entry(
            session, admin.team_id, project, keyword, updated_by=f"admin:{admin.owner_name}"
        )
        msg = f"Deleted {keyword}."
    except ServiceError:
        msg = f"{keyword} not found."
    return RedirectResponse(
        f"/admin/projects/{quote(project, safe='')}?msg={quote(msg, safe='')}", status_code=303
    )


@admin_router.post("/memory/{keyword}/compact", response_model=None)
async def memory_compact(
    request: Request,
    keyword: str,
    project: str,
    csrf: str | None = Form(default=None),
    admin: AdminSession = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> PlainTextResponse | RedirectResponse:
    if not check_csrf(request, csrf):
        return PlainTextResponse("bad csrf", status_code=400)
    got = await get_entries(session, admin.team_id, project, [keyword])
    if not got["entries"]:
        msg = f"{keyword} not found."
        return RedirectResponse(
            f"/admin/projects/{quote(project, safe='')}?msg={quote(msg, safe='')}",
            status_code=303,
        )
    entry = got["entries"][0]
    if not entry["fragments"]:
        msg = "No pending fragments."
        return RedirectResponse(
            f"/admin/memory/{quote(keyword, safe='')}"
            f"?project={quote(project, safe='')}&msg={quote(msg, safe='')}",
            status_code=303,
        )
    merged = entry["content"] + "\n\n" + "\n\n".join(f["content"] for f in entry["fragments"])
    try:
        await compact_entry(
            session,
            admin.team_id,
            project,
            keyword,
            content=merged,
            expected_version=entry["version"],
            last_fragment_id=entry["last_fragment_id"],
            updated_by=f"admin:{admin.owner_name}",
        )
        msg = f"Compacted {keyword}."
    except ServiceError as exc:
        msg = f"Compaction failed: {exc.message}"
    return RedirectResponse(
        f"/admin/memory/{quote(keyword, safe='')}"
        f"?project={quote(project, safe='')}&msg={quote(msg, safe='')}",
        status_code=303,
    )


@admin_router.get("/reports/stale", response_class=HTMLResponse)
async def stale_report(
    request: Request,
    admin: AdminSession = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    s = get_settings()
    rows = await stale_entries(session, admin.team_id, s.stale_report_days)
    return templates.TemplateResponse(
        request,
        "report_stale.html",
        {"admin": admin, "rows": rows, "min_age_days": s.stale_report_days},
    )


@admin_router.get("/reports/duplicates", response_class=HTMLResponse)
async def duplicates_report(
    request: Request,
    admin: AdminSession = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    s = get_settings()
    rows = await duplicate_pairs(session, admin.team_id, s.near_duplicate_threshold)
    return templates.TemplateResponse(
        request,
        "report_duplicates.html",
        {"admin": admin, "rows": rows, "threshold": s.near_duplicate_threshold},
    )

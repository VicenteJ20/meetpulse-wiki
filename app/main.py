from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
import re

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from app.content import normalize_scope_identifier, validate_identifier
from app.service import ContextConflict, SourceAlreadyExists, WikiService, WorkStoreRequired
from app.storage import ObjectNotFound, ObjectStorage, R2Storage, StorageError
from app.auth import GoogleTokenVerifier
from app.config import CorsSettings, Settings
from app.identity import D1Store, IdentityStore, User
from app.jobs import D1JobStore, JobStore, job_id_for_source
from app.librarian import InvalidLibrarianSignature, verify_librarian_signature
from app.work import (
    D1WorkStore, NotAllowed, NotFound, RequestConflict, WorkStore, build_pending_view, build_week_view,
    public_commitment, validate_due, validate_timezone, validate_week, week_of, NOTE_BODY_LIMIT, REQUEST_RE,
)


logger = logging.getLogger(__name__)


def create_app(
    storage: ObjectStorage | None = None,
    identity: IdentityStore | None = None,
    verifier: GoogleTokenVerifier | None = None,
    jobs: JobStore | None = None,
    work: WorkStore | None = None,
    *,
    require_raw_source: bool | None = None,
    librarian_secret: str | None = None,
) -> FastAPI:
    app = FastAPI(title="MeetPulse Wiki API", version="1.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CorsSettings().allowed_origins(),
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["content-type", "authorization"],
    )
    app.state.storage = storage
    app.state.identity = identity
    app.state.verifier = verifier
    app.state.jobs = jobs
    app.state.work = work
    app.state.require_raw_source = require_raw_source
    app.state.librarian_secret = librarian_secret

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if request.method == "OPTIONS" or request.url.path in {"/docs", "/openapi.json", "/redoc"} or request.url.path.startswith("/api/v1/internal/librarian/"):
            return await call_next(request)
        from fastapi.responses import JSONResponse
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            return JSONResponse(status_code=401, content={"detail": "Bearer token required"})
        try:
            if request.app.state.verifier is None:
                settings = Settings()
                request.app.state.verifier = GoogleTokenVerifier(settings.google_oauth_audiences())
            current_verifier = request.app.state.verifier
            request.state.user = current_verifier.verify(header[7:])
            tenant_match = re.match(r"/api/v1/(?:tree|logs|dashboard|wiki|jobs)/([^/]+)", request.url.path)
            if tenant_match:
                active_store = request.app.state.identity
                if active_store is None:
                    settings = Settings(); active_store = D1Store(settings.cloudflare_account_id, settings.cloudflare_d1_database_id, settings.cloudflare_d1_api_token); request.app.state.identity = active_store
                if active_store.role_for(request.state.user, tenant_match.group(1)) is None:
                    return JSONResponse(status_code=403, content={"detail": "You do not have access to this tenant"})
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        return await call_next(request)

    def service(request: Request) -> WikiService:
        if request.app.state.storage is None:
            request.app.state.storage = R2Storage.from_environment()
        return WikiService(request.app.state.storage)

    def identities(request: Request) -> IdentityStore:
        if request.app.state.identity is None:
            settings = Settings()
            request.app.state.identity = D1Store(settings.cloudflare_account_id, settings.cloudflare_d1_database_id, settings.cloudflare_d1_api_token)
        return request.app.state.identity

    def job_store(request: Request) -> JobStore:
        if request.app.state.jobs is not None:
            return request.app.state.jobs
        active_identity = identities(request)
        if not isinstance(active_identity, D1Store):
            raise HTTPException(status_code=503, detail="Librarian job store is not configured")
        request.app.state.jobs = D1JobStore(active_identity)
        return request.app.state.jobs

    def work_store(request: Request, *, required: bool) -> WorkStore | None:
        if request.app.state.work is not None:
            return request.app.state.work
        active = request.app.state.identity
        if active is None:
            settings = Settings()
            active = D1Store(settings.cloudflare_account_id, settings.cloudflare_d1_database_id, settings.cloudflare_d1_api_token)
            request.app.state.identity = active
        if not isinstance(active, D1Store):
            if required:
                raise HTTPException(status_code=503, detail="Work store is not configured")
            return None
        request.app.state.work = D1WorkStore(active)
        return request.app.state.work

    def raw_is_required(request: Request) -> bool:
        configured = request.app.state.require_raw_source
        return Settings().require_raw_source if configured is None else configured

    def webhook_secret(request: Request) -> str:
        configured = request.app.state.librarian_secret
        return Settings().librarian_webhook_secret if configured is None else configured

    def current_user(request: Request) -> User: return request.state.user
    def require_owner(request: Request, tenant_id: str) -> None:
        if identities(request).role_for(current_user(request), tenant_id) != "owner":
            raise HTTPException(status_code=403, detail="Only the tenant owner can manage users")

    async def json_object(request: Request) -> dict:
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=422, detail="Request body must be JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="Request body must be an object")
        return payload

    def checked_note_id(note_id: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{32}", note_id):
            raise HTTPException(status_code=422, detail="invalid note id")
        return note_id

    def require_member(request: Request, tenant_id: str) -> None:
        if identities(request).role_for(current_user(request), tenant_id) is None:
            raise HTTPException(status_code=403, detail="You do not have access to this tenant")

    def scope_exists(request: Request, tenant_id: str, client_id: str | None, project_id: str | None) -> None:
        if project_id and not client_id:
            raise HTTPException(status_code=422, detail="client_id is required when project_id is provided")
        if not client_id:
            return
        wiki = service(request)
        if project_id:
            prefixes = (f"wiki/{tenant_id}/{client_id}/{project_id}/", f"sources/{tenant_id}/{client_id}/{project_id}/")
        else:
            prefixes = (f"wiki/{tenant_id}/{client_id}/", f"sources/{tenant_id}/{client_id}/")
        if not any(wiki.storage.list_keys(prefix) for prefix in prefixes):
            raise HTTPException(status_code=422, detail="client or project does not exist")

    def text_field(payload: dict, field: str, *, required: bool, limit: int) -> str | None:
        if field not in payload or payload[field] is None:
            if required:
                raise HTTPException(status_code=422, detail=f"{field} is required")
            return None
        value = payload[field]
        if not isinstance(value, str):
            raise HTTPException(status_code=422, detail=f"{field} must be a string")
        stripped = value.strip()
        if required and not stripped:
            raise HTTPException(status_code=422, detail=f"{field} is required")
        if len(stripped) > limit:
            raise HTTPException(status_code=422, detail=f"{field} is too long")
        return stripped

    def optional_scope(payload: dict, field: str) -> str | None:
        if field not in payload or payload[field] in {None, ""}:
            return None
        return identifier(str(payload[field]), field)

    def identifier(value: str, field: str) -> str:
        try:
            return normalize_scope_identifier(value, field) if field in {"client_id", "project_id"} else validate_identifier(value, field)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/ingest", status_code=201)
    async def ingest(
        request: Request,
        file: UploadFile = File(...),
        raw_file: UploadFile | None = File(None),
        tenant_id: str = Form(...), client_id: str = Form(...), project_id: str = Form(...),
        title: str = Form(...), date_time: datetime = Form(...), participants: list[str] = Form(...),
    ) -> dict[str, object]:
        tenant_id, client_id, project_id = (
            identifier(tenant_id, "tenant_id"), identifier(client_id, "client_id"), identifier(project_id, "project_id")
        )
        if identities(request).role_for(current_user(request), tenant_id) is None:
            raise HTTPException(status_code=403, detail="You do not have access to this tenant")
        if not file.filename or not file.filename.lower().endswith(".md"):
            raise HTTPException(status_code=422, detail="file must have a .md extension")
        try:
            markdown = (await file.read()).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=422, detail="file must be UTF-8") from exc
        if not markdown.strip():
            raise HTTPException(status_code=422, detail="file must not be empty")
        if raw_is_required(request) and raw_file is None:
            raise HTTPException(status_code=422, detail="raw_file is required")
        raw_data: bytes | None = None
        if raw_file is not None:
            if not raw_file.filename:
                raise HTTPException(status_code=422, detail="raw_file must have a filename")
            raw_data = await raw_file.read()
            if not raw_data:
                raise HTTPException(status_code=422, detail="raw_file must not be empty")
            try:
                raw_data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HTTPException(status_code=422, detail="raw_file must be UTF-8") from exc
        try:
            result = service(request).ingest(
                tenant_id=tenant_id, client_id=client_id, project_id=project_id,
                title=title, date_time=date_time, participants=participants, markdown=markdown,
                raw_data=raw_data, raw_filename=raw_file.filename if raw_file else None,
                raw_content_type=raw_file.content_type or "text/plain; charset=utf-8" if raw_file else "text/plain; charset=utf-8",
            )
            job_id = job_id_for_source(str(result["source_key"]))
            try:
                job_store(request).create_pending(
                    job_id=job_id, source_key=str(result["source_key"]), tenant_id=tenant_id,
                    client_id=client_id, project_id=project_id,
                )
            except Exception as exc:
                # R2 notifications reconstruct missing jobs, so a transient D1
                # failure must not turn a successful immutable ingest into a 500.
                logger.warning("Could not create pending librarian job (%s)", type(exc).__name__)
            return {**result, "job_id": job_id, "processing_status": "pending"}
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except SourceAlreadyExists as exc:
            raise HTTPException(status_code=409, detail={"source_key": str(exc)}) from exc
        except StorageError as exc:
            raise HTTPException(status_code=502, detail="R2 storage operation failed") from exc

    @app.get("/api/v1/jobs/{tenant_id}")
    def list_jobs(
        request: Request, tenant_id: str, client_id: str | None = None, project_id: str | None = None,
        limit: int = Query(50, ge=1, le=100),
    ) -> dict[str, object]:
        tenant_id = identifier(tenant_id, "tenant_id")
        if client_id: client_id = identifier(client_id, "client_id")
        if project_id:
            if not client_id: raise HTTPException(status_code=422, detail="client_id is required when project_id is provided")
            project_id = identifier(project_id, "project_id")
        return {"items": job_store(request).list(tenant_id, client_id=client_id, project_id=project_id, limit=limit)}

    @app.get("/api/v1/jobs/{tenant_id}/{job_id}")
    def get_job(request: Request, tenant_id: str, job_id: str) -> dict[str, object]:
        tenant_id = identifier(tenant_id, "tenant_id")
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise HTTPException(status_code=422, detail="invalid job_id")
        job = job_store(request).get(tenant_id, job_id)
        if job is None: raise HTTPException(status_code=404, detail="Librarian job not found")
        return job

    async def signed_payload(request: Request) -> dict[str, object]:
        body = await request.body()
        try:
            verify_librarian_signature(
                body=body, timestamp=request.headers.get("x-librarian-timestamp"),
                signature=request.headers.get("x-librarian-signature"), secret=webhook_secret(request),
            )
        except InvalidLibrarianSignature as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=422, detail="Request body must be JSON") from exc
        if not isinstance(payload, dict): raise HTTPException(status_code=422, detail="Request body must be an object")
        return payload

    @app.post("/api/v1/internal/librarian/apply")
    async def apply_librarian(request: Request) -> dict[str, object]:
        payload = await signed_payload(request)
        try:
            for field in ("tenant_id", "client_id", "project_id"):
                if field not in payload: raise ValueError(f"{field} is required")
                payload[field] = validate_identifier(str(payload[field]), field)
            commitments = payload.get("commitments") or []
            store = work_store(request, required=bool(commitments))
            return service(request).apply_librarian(payload, work=store)
        except WorkStoreRequired as exc:
            raise HTTPException(status_code=503, detail="Work store is not configured") from exc
        except ContextConflict as exc:
            raise HTTPException(status_code=409, detail={"context_key": str(exc)}) from exc
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ObjectNotFound as exc:
            raise HTTPException(status_code=404, detail={"key": str(exc)}) from exc
        except StorageError as exc:
            raise HTTPException(status_code=502, detail="R2 storage operation failed") from exc

    @app.post("/api/v1/internal/librarian/maintenance")
    async def maintain_librarian(request: Request) -> dict[str, object]:
        payload = await signed_payload(request)
        try:
            tenant_id = validate_identifier(str(payload["tenant_id"]), "tenant_id")
            return service(request).maintain_tenant(tenant_id)
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except StorageError as exc:
            raise HTTPException(status_code=502, detail="R2 storage operation failed") from exc

    @app.get("/api/v1/tree/{tenant_id}")
    def tree(request: Request, tenant_id: str, client_id: str | None = None, project_id: str | None = None) -> dict[str, object]:
        tenant_id = identifier(tenant_id, "tenant_id")
        if client_id:
            client_id = identifier(client_id, "client_id")
        if project_id:
            project_id = identifier(project_id, "project_id")
        try:
            return service(request).tree(tenant_id, client_id, project_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ObjectNotFound as exc:
            raise HTTPException(status_code=404, detail={"key": str(exc)}) from exc
        except StorageError as exc:
            raise HTTPException(status_code=502, detail="R2 storage operation failed") from exc

    @app.get("/api/v1/logs/{tenant_id}")
    def logs(request: Request, tenant_id: str, limit: int = Query(100, ge=1, le=500)) -> dict[str, object]:
        tenant_id = identifier(tenant_id, "tenant_id")
        try:
            return service(request).logs(tenant_id, limit)
        except ObjectNotFound as exc:
            raise HTTPException(status_code=404, detail={"key": str(exc)}) from exc
        except StorageError as exc:
            raise HTTPException(status_code=502, detail="R2 storage operation failed") from exc

    @app.get("/api/v1/dashboard/{tenant_id}/summary")
    def dashboard_summary(request: Request, tenant_id: str) -> dict[str, object]:
        tenant_id = identifier(tenant_id, "tenant_id")
        try:
            return service(request).dashboard_summary(tenant_id)
        except StorageError as exc:
            raise HTTPException(status_code=502, detail="R2 storage operation failed") from exc

    @app.get("/api/v1/dashboard/{tenant_id}/clients")
    def dashboard_clients(request: Request, tenant_id: str, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)) -> dict[str, object]:
        tenant_id = identifier(tenant_id, "tenant_id")
        try:
            return service(request).dashboard_clients(tenant_id, limit, offset)
        except StorageError as exc:
            raise HTTPException(status_code=502, detail="R2 storage operation failed") from exc

    @app.get("/api/v1/dashboard/{tenant_id}/clients/{client_id}/projects")
    def dashboard_projects(request: Request, tenant_id: str, client_id: str, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)) -> dict[str, object]:
        tenant_id, client_id = identifier(tenant_id, "tenant_id"), identifier(client_id, "client_id")
        try:
            return service(request).dashboard_projects(tenant_id, client_id, limit, offset)
        except StorageError as exc:
            raise HTTPException(status_code=502, detail="R2 storage operation failed") from exc

    @app.get("/api/v1/dashboard/{tenant_id}/activity")
    def dashboard_activity(request: Request, tenant_id: str, limit: int = Query(20, ge=1, le=500)) -> dict[str, object]:
        tenant_id = identifier(tenant_id, "tenant_id")
        try:
            return service(request).activity(tenant_id, limit)
        except StorageError as exc:
            raise HTTPException(status_code=502, detail="R2 storage operation failed") from exc

    @app.get("/api/v1/wiki/{tenant_id}/documents")
    def list_documents(
        request: Request, tenant_id: str, client_id: str | None = None, project_id: str | None = None,
        document_type: list[str] | None = Query(None), limit: int | None = Query(None, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ) -> dict[str, object]:
        tenant_id = identifier(tenant_id, "tenant_id")
        if client_id:
            client_id = identifier(client_id, "client_id")
        if project_id:
            project_id = identifier(project_id, "project_id")
        try:
            invalid_types = sorted(set(document_type or ()) - {"context", "meeting", "decision", "risk"})
            if invalid_types: raise ValueError(f"invalid document types: {', '.join(invalid_types)}")
            return service(request).list_documents(tenant_id, client_id, project_id, document_types=set(document_type or ()), limit=limit, offset=offset)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ObjectNotFound as exc:
            raise HTTPException(status_code=404, detail={"key": str(exc)}) from exc
        except StorageError as exc:
            raise HTTPException(status_code=502, detail="R2 storage operation failed") from exc

    @app.get("/api/v1/wiki/{tenant_id}/documents/{document}/raw")
    def read_transcript(
        request: Request, tenant_id: str, document: str, client_id: str | None = None,
        project_id: str | None = None, file: str | None = None,
    ) -> dict[str, object]:
        tenant_id = identifier(tenant_id, "tenant_id")
        if client_id:
            client_id = identifier(client_id, "client_id")
        if project_id:
            project_id = identifier(project_id, "project_id")
        try:
            return service(request).read_transcript(tenant_id, client_id, project_id, document, file)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ObjectNotFound as exc:
            raise HTTPException(status_code=404, detail={"key": str(exc)}) from exc
        except StorageError as exc:
            raise HTTPException(status_code=502, detail="R2 storage operation failed") from exc

    @app.get("/api/v1/wiki/{tenant_id}/documents/{document}")
    def read_document(request: Request, tenant_id: str, document: str, client_id: str | None = None, project_id: str | None = None) -> dict[str, object]:
        tenant_id = identifier(tenant_id, "tenant_id")
        if client_id:
            client_id = identifier(client_id, "client_id")
        if project_id:
            project_id = identifier(project_id, "project_id")
        try:
            return service(request).read_document(tenant_id, client_id, project_id, document)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ObjectNotFound as exc:
            raise HTTPException(status_code=404, detail={"key": str(exc)}) from exc
        except StorageError as exc:
            raise HTTPException(status_code=502, detail="R2 storage operation failed") from exc

    @app.put("/api/v1/wiki/{tenant_id}/documents/context")
    def update_project_context(
        request: Request, tenant_id: str, content_markdown: str = Body(..., embed=True),
        client_id: str | None = None, project_id: str | None = None,
    ) -> dict[str, object]:
        tenant_id = identifier(tenant_id, "tenant_id")
        if not client_id or not project_id:
            raise HTTPException(status_code=422, detail="client_id and project_id are required for project context")
        client_id, project_id = identifier(client_id, "client_id"), identifier(project_id, "project_id")
        try:
            return service(request).update_context(tenant_id, client_id, project_id, content_markdown)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ObjectNotFound as exc:
            raise HTTPException(status_code=404, detail={"key": str(exc)}) from exc
        except StorageError as exc:
            raise HTTPException(status_code=502, detail="R2 storage operation failed") from exc

    @app.get("/api/v1/tenants/availability")
    def tenant_availability(request: Request, name: str = Query(...)) -> dict[str, object]:
        try:
            return identities(request).tenant_available(name)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/tenants/provision", status_code=201)
    async def provision_tenant(request: Request) -> dict[str, object]:
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=422, detail="Request body must be JSON with a tenant name") from exc
        name = payload.get("name") if isinstance(payload, dict) else None
        if not isinstance(name, str):
            raise HTTPException(status_code=422, detail="A tenant name is required")
        try:
            return identities(request).provision(current_user(request), name)
        except ValueError as exc:
            status_code = 409 if "already in use" in str(exc) else 422
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    @app.get("/api/v1/tenants")
    def list_tenants(request: Request) -> dict[str, object]:
        return {"items": identities(request).tenants_for(current_user(request))}

    @app.get("/api/v1/me/settings")
    def my_settings(request: Request) -> dict[str, object]:
        store = work_store(request, required=True)
        assert store is not None
        return {"timezone": store.timezone_for(current_user(request).google_sub)}

    @app.patch("/api/v1/me/settings")
    async def update_my_settings(request: Request) -> dict[str, object]:
        payload = await json_object(request)
        try:
            timezone = validate_timezone(str(payload.get("timezone", "")))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        store = work_store(request, required=True)
        assert store is not None
        return store.set_timezone(current_user(request).google_sub, timezone)

    @app.get("/api/v1/me/week")
    def my_week(request: Request, tenant_id: str = Query(...), week: str | None = None) -> dict[str, object]:
        tenant_id = identifier(tenant_id, "tenant_id")
        require_member(request, tenant_id)
        store = work_store(request, required=True)
        assert store is not None
        user = current_user(request)
        timezone = store.timezone_for(user.google_sub)
        try:
            selected = validate_week(week) if week else week_of(datetime.now(UTC), timezone)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        rows = [public_commitment(row, user.google_sub) for row in store.visible_commitments(tenant_id, user.google_sub, None)]
        return {"tenant_id": tenant_id, **build_week_view(rows, timezone=timezone, week=selected)}

    @app.get("/api/v1/tenants/{tenant_id}/pending")
    def list_pending(request: Request, tenant_id: str, client_id: str | None = None) -> dict[str, object]:
        tenant_id = identifier(tenant_id, "tenant_id")
        if client_id:
            client_id = identifier(client_id, "client_id")
        require_member(request, tenant_id)
        store = work_store(request, required=True)
        assert store is not None
        user = current_user(request)
        wiki = service(request)
        rows = [public_commitment(row, user.google_sub) for row in store.visible_commitments(tenant_id, user.google_sub, client_id)]
        return build_pending_view(
            rows, tenant_id=tenant_id, timezone=store.timezone_for(user.google_sub), now=datetime.now(UTC),
            client_id=client_id,
            decision_digest=lambda client, project: wiki.decision_digest(tenant_id, client, project),
            latest_meeting=lambda client, project: wiki._latest_meeting(tenant_id, client, project),
        )

    @app.post("/api/v1/tenants/{tenant_id}/commitments", status_code=201)
    async def create_commitment(request: Request, tenant_id: str) -> dict[str, object]:
        tenant_id = identifier(tenant_id, "tenant_id")
        require_member(request, tenant_id)
        payload = await json_object(request)
        title = text_field(payload, "title", required=True, limit=200)
        detail = text_field(payload, "detail", required=False, limit=4000) or ""
        client_id = optional_scope(payload, "client_id")
        project_id = optional_scope(payload, "project_id")
        scope_exists(request, tenant_id, client_id, project_id)
        origin = payload.get("origin") or "manual"
        if origin not in {"manual", "mcp"}:
            raise HTTPException(status_code=422, detail="origin must be manual or mcp")
        week = payload.get("week")
        due_on = payload.get("due_on")
        try:
            if week not in {None, ""}:
                week = validate_week(str(week))
            else:
                week = None
            due_on = validate_due(None if due_on in {None, ""} else str(due_on))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        request_id = payload.get("client_request_id")
        if request_id is not None and (not isinstance(request_id, str) or not REQUEST_RE.fullmatch(request_id)):
            raise HTTPException(status_code=422, detail="client_request_id must be 8 to 80 letters, numbers, underscores or hyphens")
        assignee = text_field(payload, "assignee_label", required=False, limit=120)
        store = work_store(request, required=True)
        assert store is not None
        user = current_user(request)
        try:
            created = store.create_commitment(owner_sub=user.google_sub, tenant_id=tenant_id, fields={
                "title": title, "detail": detail, "client_id": client_id, "project_id": project_id,
                "week": week, "due_on": due_on, "origin": origin, "client_request_id": request_id,
                "assignee_label": assignee,
            })
        except RequestConflict as exc:
            raise HTTPException(status_code=409, detail="client_request_id was already used for a different commitment") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return public_commitment(created, user.google_sub)

    @app.patch("/api/v1/tenants/{tenant_id}/commitments/{commitment_id}")
    async def update_commitment(request: Request, tenant_id: str, commitment_id: str) -> dict[str, object]:
        tenant_id = identifier(tenant_id, "tenant_id")
        if not re.fullmatch(r"[a-f0-9]{32}", commitment_id):
            raise HTTPException(status_code=422, detail="invalid commitment id")
        require_member(request, tenant_id)
        payload = await json_object(request)
        allowed = {"status", "week", "title", "detail", "client_id", "project_id", "due_on"}
        unknown = set(payload) - allowed
        if unknown:
            raise HTTPException(status_code=422, detail=f"unknown fields: {', '.join(sorted(unknown))}")
        changes: dict[str, object] = {}
        if "title" in payload:
            changes["title"] = text_field(payload, "title", required=True, limit=200)
        if "detail" in payload:
            changes["detail"] = text_field(payload, "detail", required=False, limit=4000) or ""
        if "status" in payload:
            if payload["status"] not in {"open", "done", "dropped"}:
                raise HTTPException(status_code=422, detail="status must be open, done, or dropped")
            changes["status"] = payload["status"]
        try:
            if "week" in payload:
                changes["week"] = validate_week(str(payload["week"]))
            if "due_on" in payload:
                changes["due_on"] = validate_due(None if payload["due_on"] in {None, ""} else str(payload["due_on"]))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if "client_id" in payload:
            changes["client_id"] = optional_scope(payload, "client_id")
        if "project_id" in payload:
            changes["project_id"] = optional_scope(payload, "project_id")
        store = work_store(request, required=True)
        assert store is not None
        user = current_user(request)
        current = next((row for row in store.visible_commitments(tenant_id, user.google_sub, None) if row["id"] == commitment_id), None)
        if current is None:
            raise HTTPException(status_code=404, detail="Commitment not found")
        client_id = changes["client_id"] if "client_id" in changes else current.get("client_id")
        project_id = changes["project_id"] if "project_id" in changes else current.get("project_id")
        scope_exists(request, tenant_id, client_id if isinstance(client_id, str) else None, project_id if isinstance(project_id, str) else None)
        try:
            updated = store.update_commitment(actor_sub=user.google_sub, tenant_id=tenant_id, commitment_id=commitment_id, changes=changes)
        except NotAllowed as exc:
            raise HTTPException(status_code=403, detail="You cannot change another member's commitment") from exc
        except NotFound as exc:
            raise HTTPException(status_code=404, detail="Commitment not found") from exc
        return public_commitment(updated, user.google_sub)

    @app.post("/api/v1/tenants/{tenant_id}/notes", status_code=201)
    async def create_note(request: Request, tenant_id: str) -> dict[str, object]:
        tenant_id = identifier(tenant_id, "tenant_id")
        require_member(request, tenant_id)
        payload = await json_object(request)
        return save_note(request, tenant_id, payload, None)

    @app.get("/api/v1/tenants/{tenant_id}/notes")
    def list_notes(
        request: Request, tenant_id: str, client_id: str | None = None, project_id: str | None = None,
        q: str | None = None, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
    ) -> dict[str, object]:
        tenant_id = identifier(tenant_id, "tenant_id")
        if client_id:
            client_id = identifier(client_id, "client_id")
        if project_id:
            if not client_id:
                raise HTTPException(status_code=422, detail="client_id is required when project_id is provided")
            project_id = identifier(project_id, "project_id")
        require_member(request, tenant_id)
        if q is not None and not q.strip():
            raise HTTPException(status_code=422, detail="q must not be empty")
        store = work_store(request, required=True)
        assert store is not None
        return store.list_notes(
            author_sub=current_user(request).google_sub, tenant_id=tenant_id, client_id=client_id,
            project_id=project_id, query=q.strip() if q else None, limit=limit, offset=offset,
        )

    @app.get("/api/v1/tenants/{tenant_id}/notes/{note_id}")
    def read_note(request: Request, tenant_id: str, note_id: str) -> dict[str, object]:
        tenant_id = identifier(tenant_id, "tenant_id")
        require_member(request, tenant_id)
        return load_note(request, tenant_id, note_id)

    @app.patch("/api/v1/tenants/{tenant_id}/notes/{note_id}")
    async def update_note(request: Request, tenant_id: str, note_id: str) -> dict[str, object]:
        tenant_id = identifier(tenant_id, "tenant_id")
        require_member(request, tenant_id)
        payload = await json_object(request)
        return save_note(request, tenant_id, payload, note_id)

    @app.delete("/api/v1/tenants/{tenant_id}/notes/{note_id}", status_code=204)
    def delete_note(request: Request, tenant_id: str, note_id: str) -> None:
        tenant_id = identifier(tenant_id, "tenant_id")
        require_member(request, tenant_id)
        store = work_store(request, required=True)
        assert store is not None
        try:
            store.delete_note(author_sub=current_user(request).google_sub, tenant_id=tenant_id, note_id=checked_note_id(note_id))
        except NotFound as exc:
            raise HTTPException(status_code=404, detail="Note not found") from exc

    def save_note(request: Request, tenant_id: str, payload: dict, note_id: str | None) -> dict[str, object]:
        store = work_store(request, required=True)
        assert store is not None
        user = current_user(request)
        if note_id is None:
            body = text_field(payload, "body", required=True, limit=NOTE_BODY_LIMIT)
            title = text_field(payload, "title", required=False, limit=200)
            client_id = optional_scope(payload, "client_id")
            project_id = optional_scope(payload, "project_id")
            scope_exists(request, tenant_id, client_id, project_id)
            source = payload.get("source") or "manual"
            if source not in {"manual", "mcp"}:
                raise HTTPException(status_code=422, detail="source must be manual or mcp")
            if "pinned" in payload and not isinstance(payload["pinned"], bool):
                raise HTTPException(status_code=422, detail="pinned must be a boolean")
            request_id = payload.get("client_request_id")
            if request_id is not None and (not isinstance(request_id, str) or not REQUEST_RE.fullmatch(request_id)):
                raise HTTPException(status_code=422, detail="client_request_id must be 8 to 80 letters, numbers, underscores or hyphens")
            try:
                return store.create_note(author_sub=user.google_sub, tenant_id=tenant_id, fields={
                    "title": title, "body": body, "client_id": client_id, "project_id": project_id,
                    "pinned": bool(payload.get("pinned")), "source": source, "client_request_id": request_id,
                })
            except RequestConflict as exc:
                raise HTTPException(status_code=409, detail="client_request_id was already used for a different note") from exc
        changes: dict[str, object] = {}
        allowed = {"title", "body", "client_id", "project_id", "pinned"}
        unknown = set(payload) - allowed
        if unknown:
            raise HTTPException(status_code=422, detail=f"unknown fields: {', '.join(sorted(unknown))}")
        if "body" in payload:
            changes["body"] = text_field(payload, "body", required=True, limit=NOTE_BODY_LIMIT)
        if "title" in payload:
            changes["title"] = text_field(payload, "title", required=False, limit=200)
        if "pinned" in payload:
            if not isinstance(payload["pinned"], bool):
                raise HTTPException(status_code=422, detail="pinned must be a boolean")
            changes["pinned"] = payload["pinned"]
        if "client_id" in payload:
            changes["client_id"] = optional_scope(payload, "client_id")
        if "project_id" in payload:
            changes["project_id"] = optional_scope(payload, "project_id")
        current = load_note(request, tenant_id, note_id)
        client_id = changes["client_id"] if "client_id" in changes else current.get("client_id")
        project_id = changes["project_id"] if "project_id" in changes else current.get("project_id")
        scope_exists(request, tenant_id, client_id if isinstance(client_id, str) else None, project_id if isinstance(project_id, str) else None)
        return store.update_note(author_sub=user.google_sub, tenant_id=tenant_id, note_id=checked_note_id(note_id), changes=changes)

    def load_note(request: Request, tenant_id: str, note_id: str) -> dict[str, object]:
        store = work_store(request, required=True)
        assert store is not None
        try:
            return store.get_note(author_sub=current_user(request).google_sub, tenant_id=tenant_id, note_id=checked_note_id(note_id))
        except NotFound as exc:
            raise HTTPException(status_code=404, detail="Note not found") from exc

    @app.get("/api/v1/me/invitations")
    def my_invitations(request: Request) -> dict[str, object]:
        return {"items": identities(request).pending(current_user(request))}

    @app.post("/api/v1/me/invitations/{invitation_id}/accept")
    def accept_invitation(request: Request, invitation_id: str) -> dict[str, object]:
        try:
            return identities(request).respond(current_user(request), invitation_id, "accepted")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/me/invitations/{invitation_id}/reject")
    def reject_invitation(request: Request, invitation_id: str) -> dict[str, object]:
        try:
            return identities(request).respond(current_user(request), invitation_id, "rejected")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/tenants/{tenant_id}/members")
    def list_members(request: Request, tenant_id: str) -> dict[str, object]:
        tenant_id = identifier(tenant_id, "tenant_id"); require_owner(request, tenant_id)
        return {"items": identities(request).members(current_user(request), tenant_id)}

    @app.post("/api/v1/tenants/{tenant_id}/invitations", status_code=201)
    def create_invitation(request: Request, tenant_id: str, email: str = Body(..., embed=True)) -> dict[str, object]:
        tenant_id = identifier(tenant_id, "tenant_id"); require_owner(request, tenant_id)
        if "@" not in email or len(email) > 320:
            raise HTTPException(status_code=422, detail="A valid email is required")
        return identities(request).invite(current_user(request), tenant_id, email)

    @app.get("/api/v1/tenants/{tenant_id}/invitations")
    def list_invitations(request: Request, tenant_id: str) -> dict[str, object]:
        tenant_id = identifier(tenant_id, "tenant_id"); require_owner(request, tenant_id)
        return {"items": identities(request).invitations(current_user(request), tenant_id)}

    @app.delete("/api/v1/tenants/{tenant_id}/members/{member_sub}", status_code=204)
    def revoke_member(request: Request, tenant_id: str, member_sub: str) -> None:
        tenant_id = identifier(tenant_id, "tenant_id"); require_owner(request, tenant_id)
        identities(request).revoke(current_user(request), tenant_id, member_sub)

    return app


app = create_app()

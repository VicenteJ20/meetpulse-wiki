from __future__ import annotations

from datetime import datetime
import json
import logging
import re

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from app.content import validate_identifier
from app.service import ContextConflict, SourceAlreadyExists, WikiService
from app.storage import ObjectNotFound, ObjectStorage, R2Storage, StorageError
from app.auth import GoogleTokenVerifier
from app.config import CorsSettings, Settings
from app.identity import D1Store, IdentityStore, User
from app.jobs import D1JobStore, JobStore, job_id_for_source
from app.librarian import InvalidLibrarianSignature, verify_librarian_signature


logger = logging.getLogger(__name__)


def create_app(
    storage: ObjectStorage | None = None,
    identity: IdentityStore | None = None,
    verifier: GoogleTokenVerifier | None = None,
    jobs: JobStore | None = None,
    *,
    require_raw_source: bool | None = None,
    librarian_secret: str | None = None,
) -> FastAPI:
    app = FastAPI(title="MeetPulse Wiki API", version="1.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CorsSettings().allowed_origins(),
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["content-type", "authorization"],
    )
    app.state.storage = storage
    app.state.identity = identity
    app.state.verifier = verifier
    app.state.jobs = jobs
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
                request.app.state.verifier = GoogleTokenVerifier(Settings().google_oauth_client_id)
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

    def identifier(value: str, field: str) -> str:
        try:
            return validate_identifier(value, field)
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
            return service(request).apply_librarian(payload)
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
    def list_documents(request: Request, tenant_id: str, client_id: str | None = None, project_id: str | None = None) -> dict[str, object]:
        tenant_id = identifier(tenant_id, "tenant_id")
        if client_id:
            client_id = identifier(client_id, "client_id")
        if project_id:
            project_id = identifier(project_id, "project_id")
        try:
            return service(request).list_documents(tenant_id, client_id, project_id)
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

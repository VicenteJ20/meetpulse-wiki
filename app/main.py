from __future__ import annotations

from datetime import datetime

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from app.content import validate_identifier
from app.service import SourceAlreadyExists, WikiService
from app.storage import ObjectNotFound, ObjectStorage, R2Storage, StorageError


def create_app(storage: ObjectStorage | None = None) -> FastAPI:
    app = FastAPI(title="MeetPulse Wiki API", version="1.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3118", "http://127.0.0.1:3118", "http://tauri.localhost", "https://tauri.localhost"],
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["content-type"],
    )
    app.state.storage = storage

    def service(request: Request) -> WikiService:
        if request.app.state.storage is None:
            request.app.state.storage = R2Storage.from_environment()
        return WikiService(request.app.state.storage)

    def identifier(value: str, field: str) -> str:
        try:
            return validate_identifier(value, field)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/ingest", status_code=201)
    async def ingest(
        request: Request,
        file: UploadFile = File(...),
        tenant_id: str = Form(...), client_id: str = Form(...), project_id: str = Form(...),
        title: str = Form(...), date_time: datetime = Form(...), participants: list[str] = Form(...),
    ) -> dict[str, object]:
        tenant_id, client_id, project_id = (
            identifier(tenant_id, "tenant_id"), identifier(client_id, "client_id"), identifier(project_id, "project_id")
        )
        if not file.filename or not file.filename.lower().endswith(".md"):
            raise HTTPException(status_code=422, detail="file must have a .md extension")
        try:
            markdown = (await file.read()).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=422, detail="file must be UTF-8") from exc
        if not markdown.strip():
            raise HTTPException(status_code=422, detail="file must not be empty")
        try:
            return service(request).ingest(tenant_id=tenant_id, client_id=client_id, project_id=project_id,
                                           title=title, date_time=date_time, participants=participants, markdown=markdown)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except SourceAlreadyExists as exc:
            raise HTTPException(status_code=409, detail={"source_key": str(exc)}) from exc
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
        except ObjectNotFound as exc:
            raise HTTPException(status_code=404, detail={"key": str(exc)}) from exc
        except StorageError as exc:
            raise HTTPException(status_code=502, detail="R2 storage operation failed") from exc

    return app


app = create_app()

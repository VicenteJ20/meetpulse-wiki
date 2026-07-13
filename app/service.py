from __future__ import annotations

import re
from datetime import UTC, datetime

from app.content import append_link, build_source_markdown, client_index, log_header, markdown_links, project_index, slugify, split_front_matter, tenant_index
from app.storage import ObjectNotFound, ObjectStorage, PreconditionFailed, StorageError


class SourceAlreadyExists(Exception):
    pass


class WikiService:
    def __init__(self, storage: ObjectStorage) -> None:
        self.storage = storage

    def ingest(self, *, tenant_id: str, client_id: str, project_id: str, title: str, date_time: datetime, participants: list[str], markdown: str) -> dict[str, object]:
        date_utc = date_time.replace(tzinfo=UTC) if date_time.tzinfo is None else date_time.astimezone(UTC)
        source_key = f"sources/{tenant_id}/{client_id}/{project_id}/{date_utc:%Y-%m-%d}-{slugify(title)}.md"
        content = build_source_markdown(markdown, tenant_id=tenant_id, client_id=client_id, project_id=project_id, title=title, date_time=date_time, participants=participants)
        try:
            self.storage.put_if_absent(source_key, content)
        except PreconditionFailed as exc:
            raise SourceAlreadyExists(source_key) from exc

        project_prefix = f"wiki/{tenant_id}/{client_id}/{project_id}"
        tenant_key, client_key, project_key = f"wiki/{tenant_id}/index.md", f"wiki/{tenant_id}/{client_id}/index.md", f"{project_prefix}/index.md"
        context_key, log_key = f"{project_prefix}/context.md", f"wiki/{tenant_id}/log.md"
        created_or_updated = [source_key]
        self._ensure(tenant_key, tenant_index(tenant_id), created_or_updated)
        self._ensure(client_key, client_index(client_id), created_or_updated)
        self._ensure(project_key, project_index(project_id), created_or_updated)
        # Empty Markdown, intentionally separate from an immutable meeting analysis.
        self._ensure(context_key, "", created_or_updated)
        self._ensure(log_key, log_header(tenant_id), created_or_updated)
        self._update(tenant_key, lambda text: append_link(text, client_id, f"{client_id}/index.md"), created_or_updated)
        self._update(client_key, lambda text: append_link(text, project_id, f"{project_id}/index.md"), created_or_updated)
        self._update(project_key, lambda text: append_link(text, title, f"/{source_key}"), created_or_updated)
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        event = f"- `{timestamp}` ingest: `/{source_key}` (client={client_id}, project={project_id}; wiki={', '.join(f'`/{key}`' for key in created_or_updated if key != source_key)})"
        self._update(log_key, lambda text: text.rstrip() + "\n" + event + "\n", created_or_updated)
        return {"source_key": source_key, "updated_keys": created_or_updated}

    def tree(self, tenant_id: str, client_id: str | None, project_id: str | None) -> dict[str, object]:
        if project_id and not client_id:
            raise ValueError("client_id is required when project_id is provided")
        key, scope = f"wiki/{tenant_id}/index.md", "tenant"
        if client_id:
            key, scope = f"wiki/{tenant_id}/{client_id}/index.md", "client"
        if project_id:
            key, scope = f"wiki/{tenant_id}/{client_id}/{project_id}/index.md", "project"
        obj = self.storage.get_text(key)
        return {"scope": scope, "key": key, "index_markdown": obj.text, "children": markdown_links(obj.text)}

    def logs(self, tenant_id: str, limit: int) -> dict[str, object]:
        key = f"wiki/{tenant_id}/log.md"
        entries = [line for line in self.storage.get_text(key).text.splitlines() if line.startswith("- `")]
        return {"key": key, "entries": list(reversed(entries))[:limit]}

    def dashboard_summary(self, tenant_id: str) -> dict[str, object]:
        wiki_keys, source_keys = self.storage.list_keys(f"wiki/{tenant_id}/"), self.storage.list_keys(f"sources/{tenant_id}/")
        events = self._activity_entries(tenant_id)
        return {"tenant_id": tenant_id, "client_count": len(self._client_ids(tenant_id, wiki_keys)), "project_count": len(self._project_index_keys(tenant_id, wiki_keys)), "source_count": len([key for key in source_keys if key.endswith(".md")]), "wiki_page_count": len([key for key in source_keys if key.endswith(".md")]) + len([key for key in wiki_keys if key.endswith("/context.md")]), "last_activity_at": events[0]["timestamp"] if events else None}

    def dashboard_clients(self, tenant_id: str, limit: int, offset: int) -> dict[str, object]:
        wiki_keys, source_keys, events = self.storage.list_keys(f"wiki/{tenant_id}/"), self.storage.list_keys(f"sources/{tenant_id}/"), self._activity_entries(tenant_id)
        clients = []
        for client_id in self._client_ids(tenant_id, wiki_keys):
            projects = self._project_index_keys(tenant_id, wiki_keys, client_id)
            clients.append({"client_id": client_id, "project_count": len(projects), "source_count": len([key for key in source_keys if key.startswith(f"sources/{tenant_id}/{client_id}/")]), "last_activity_at": self._last_activity(events, client_id), "key": f"wiki/{tenant_id}/{client_id}/index.md"})
        clients.sort(key=lambda item: item["client_id"])
        return {"total": len(clients), "items": clients[offset:offset + limit], "limit": limit, "offset": offset}

    def dashboard_projects(self, tenant_id: str, client_id: str, limit: int, offset: int) -> dict[str, object]:
        wiki_keys, source_keys, events = self.storage.list_keys(f"wiki/{tenant_id}/{client_id}/"), self.storage.list_keys(f"sources/{tenant_id}/{client_id}/"), self._activity_entries(tenant_id)
        projects = []
        for key in self._project_index_keys(tenant_id, wiki_keys, client_id):
            project_id = key.split("/")[3]
            source_count = len([source for source in source_keys if source.startswith(f"sources/{tenant_id}/{client_id}/{project_id}/") and source.endswith(".md")])
            context_count = int(f"wiki/{tenant_id}/{client_id}/{project_id}/context.md" in wiki_keys)
            projects.append({"project_id": project_id, "source_count": source_count, "wiki_page_count": source_count + context_count, "last_activity_at": self._last_activity(events, client_id, project_id), "key": key})
        projects.sort(key=lambda item: item["project_id"])
        return {"total": len(projects), "items": projects[offset:offset + limit], "limit": limit, "offset": offset}

    def activity(self, tenant_id: str, limit: int) -> dict[str, object]:
        return {"entries": self._activity_entries(tenant_id)[:limit]}

    def list_documents(self, tenant_id: str, client_id: str | None, project_id: str | None) -> dict[str, object]:
        if not client_id or not project_id:
            raise ValueError("client_id and project_id are required for user-facing project files")
        source_prefix = f"sources/{tenant_id}/{client_id}/{project_id}/"
        items = []
        for key in sorted(self.storage.list_keys(source_prefix), reverse=True):
            if key.endswith(".md"):
                obj = self.storage.get_text(key)
                stem = key.rsplit("/", 1)[-1].removesuffix(".md")
                items.append({"document": f"analysis:{stem}", "title": self._analysis_title(stem), "key": key, "updated_at": self._updated_at(obj)})
        context_key = f"wiki/{tenant_id}/{client_id}/{project_id}/context.md"
        try:
            context = self.storage.get_text(context_key)
            items.append({"document": "context", "title": "Project context", "key": context_key, "updated_at": self._updated_at(context)})
        except ObjectNotFound:
            pass
        if not items:
            raise ObjectNotFound(source_prefix)
        return {"items": items}

    def read_document(self, tenant_id: str, client_id: str | None, project_id: str | None, document: str) -> dict[str, object]:
        if not client_id or not project_id:
            raise ValueError("client_id and project_id are required for user-facing project files")
        if document == "context":
            key, title, is_analysis = f"wiki/{tenant_id}/{client_id}/{project_id}/context.md", "Project context", False
        elif document.startswith("analysis:"):
            stem = document.removeprefix("analysis:")
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", stem):
                raise ValueError("invalid analysis document")
            key, title, is_analysis = f"sources/{tenant_id}/{client_id}/{project_id}/{stem}.md", self._analysis_title(stem), True
        else:
            raise ValueError("document must be a project context or meeting analysis")
        obj = self.storage.get_text(key)
        return {"document": document, "title": title, "key": key, "content_markdown": split_front_matter(obj.text)[1] if is_analysis else obj.text, "content_type": "text/markdown", "updated_at": self._updated_at(obj)}

    def update_context(self, tenant_id: str, client_id: str, project_id: str, content_markdown: str) -> dict[str, object]:
        key = f"wiki/{tenant_id}/{client_id}/{project_id}/context.md"
        for _ in range(5):
            current = self.storage.get_text(key)
            try:
                self.storage.put_if_match(key, content_markdown, current.etag)
                saved = self.storage.get_text(key)
                return {"document": "context", "title": "Project context", "key": key, "content_markdown": saved.text, "content_type": "text/markdown", "updated_at": self._updated_at(saved)}
            except PreconditionFailed:
                continue
        raise StorageError("context update conflicted repeatedly")

    @staticmethod
    def _analysis_title(stem: str) -> str:
        return stem.replace("-", " ").title()

    @staticmethod
    def _updated_at(obj) -> str | None:
        return obj.last_modified.isoformat().replace("+00:00", "Z") if obj.last_modified else None

    @staticmethod
    def _client_ids(tenant_id: str, keys: list[str]) -> list[str]:
        prefix = f"wiki/{tenant_id}/"
        return sorted({key.split("/")[2] for key in keys if key.startswith(prefix) and len(key.split("/")) == 4 and key.endswith("/index.md")})

    @staticmethod
    def _project_index_keys(tenant_id: str, keys: list[str], client_id: str | None = None) -> list[str]:
        prefix = f"wiki/{tenant_id}/{client_id}/" if client_id else f"wiki/{tenant_id}/"
        return [key for key in keys if key.startswith(prefix) and len(key.split("/")) == 5 and key.endswith("/index.md")]

    def _activity_entries(self, tenant_id: str) -> list[dict[str, str]]:
        try: entries = self.logs(tenant_id, 500)["entries"]
        except ObjectNotFound: return []
        pattern = re.compile(r"^- `([^`]+)` ingest: `/?([^`]+)` \(client=([^,]+), project=([^;]+);")
        return [{"timestamp": match.group(1), "event": "ingest", "source_key": match.group(2), "client_id": match.group(3), "project_id": match.group(4), "raw": entry} for entry in entries if (match := pattern.match(entry))]

    @staticmethod
    def _last_activity(entries: list[dict[str, str]], client_id: str, project_id: str | None = None) -> str | None:
        return next((entry["timestamp"] for entry in entries if entry["client_id"] == client_id and (project_id is None or entry["project_id"] == project_id)), None)

    def _ensure(self, key: str, text: str, changed: list[str]) -> None:
        try: self.storage.put_if_absent(key, text); changed.append(key)
        except PreconditionFailed: pass

    def _update(self, key: str, transform, changed: list[str]) -> None:
        for _ in range(5):
            current = self.storage.get_text(key); updated = transform(current.text)
            if updated == current.text: return
            try:
                self.storage.put_if_match(key, updated, current.etag)
                if key not in changed: changed.append(key)
                return
            except PreconditionFailed: continue
        raise RuntimeError(f"too much concurrent activity updating {key}")

from __future__ import annotations

import re
from datetime import UTC, datetime
from hashlib import sha256

from app.content import append_link, build_okf_document, build_source_markdown, client_index, log_header, markdown_links, parse_front_matter, project_index, slugify, split_front_matter, tenant_index, normalize_scope_identifier, validate_meeting_analysis, validate_okf_document
from app.storage import ObjectNotFound, ObjectStorage, PreconditionFailed, StorageError


class SourceAlreadyExists(Exception):
    pass


class ContextConflict(Exception):
    pass


class WikiService:
    def __init__(self, storage: ObjectStorage) -> None:
        self.storage = storage

    def ingest(
        self,
        *,
        tenant_id: str,
        client_id: str,
        project_id: str,
        title: str,
        date_time: datetime,
        participants: list[str],
        markdown: str,
        raw_data: bytes | None = None,
        raw_filename: str | None = None,
        raw_content_type: str = "text/plain; charset=utf-8",
    ) -> dict[str, object]:
        validate_meeting_analysis(markdown)
        date_utc = date_time.replace(tzinfo=UTC) if date_time.tzinfo is None else date_time.astimezone(UTC)
        source_key = f"sources/{tenant_id}/{client_id}/{project_id}/{date_utc:%Y-%m-%d}-{slugify(title)}.md"
        existing_source = None
        try:
            existing_source = self.storage.get_text(source_key)
        except ObjectNotFound:
            pass

        analysis_only_content = build_source_markdown(
            markdown,
            tenant_id=tenant_id,
            client_id=client_id,
            project_id=project_id,
            title=title,
            date_time=date_time,
            participants=participants,
            raw_keys=[],
            raw_hashes=[],
        )
        if existing_source is not None and not self._same_source_analysis(existing_source.text, analysis_only_content):
            raise SourceAlreadyExists(source_key)

        raw_keys: list[str] = []
        raw_hashes: list[str] = []
        if raw_data is not None and raw_filename:
            raw_hash = sha256(raw_data).hexdigest()
            existing_metadata = parse_front_matter(existing_source.text)[0] if existing_source is not None else {}
            existing_raw_keys = existing_metadata.get("raw_sources")
            existing_raw_hashes = existing_metadata.get("raw_sha256")
            if isinstance(existing_raw_keys, list) and existing_raw_keys:
                if not isinstance(existing_raw_hashes, list) or raw_hash not in existing_raw_hashes:
                    raise SourceAlreadyExists(source_key)
                raw_key = str(existing_raw_keys[existing_raw_hashes.index(raw_hash)])
            else:
                filename = self._safe_raw_filename(raw_filename)
                raw_key = f"raw/{tenant_id}/{client_id}/{project_id}/{date_utc:%Y-%m-%d}-{slugify(title)}/{filename}"
            try:
                self.storage.put_bytes_if_absent(raw_key, raw_data, content_type=raw_content_type, sha256_hex=raw_hash)
            except PreconditionFailed:
                existing = self.storage.get_bytes(raw_key)
                if sha256(existing.data).hexdigest() != raw_hash:
                    raise SourceAlreadyExists(raw_key)
            raw_keys.append(raw_key)
            raw_hashes.append(raw_hash)
        elif existing_source is not None:
            existing_metadata = parse_front_matter(existing_source.text)[0]
            existing_raw_keys = existing_metadata.get("raw_sources")
            existing_raw_hashes = existing_metadata.get("raw_sha256")
            if isinstance(existing_raw_keys, list) and isinstance(existing_raw_hashes, list):
                raw_keys = [str(key) for key in existing_raw_keys]
                raw_hashes = [str(value) for value in existing_raw_hashes]
        content = build_source_markdown(
            markdown,
            tenant_id=tenant_id,
            client_id=client_id,
            project_id=project_id,
            title=title,
            date_time=date_time,
            participants=participants,
            raw_keys=raw_keys,
            raw_hashes=raw_hashes,
        )
        source_action = "created"
        if existing_source is None:
            try:
                self.storage.put_if_absent(source_key, content)
            except PreconditionFailed as exc:
                raise SourceAlreadyExists(source_key) from exc
        elif existing_source.text == content:
            source_action = "unchanged"
        else:
            try:
                self.storage.put_if_match(source_key, content, existing_source.etag)
                source_action = "reconciled"
            except PreconditionFailed as exc:
                raise SourceAlreadyExists(source_key) from exc

        project_prefix = f"wiki/{tenant_id}/{client_id}/{project_id}"
        tenant_key, client_key, project_key = f"wiki/{tenant_id}/index.md", f"wiki/{tenant_id}/{client_id}/index.md", f"{project_prefix}/index.md"
        context_key, log_key = f"{project_prefix}/context.md", f"wiki/{tenant_id}/log.md"
        created_or_updated = [] if source_action == "unchanged" else [source_key]
        self._ensure(tenant_key, tenant_index(tenant_id), created_or_updated)
        self._ensure(client_key, client_index(client_id), created_or_updated)
        self._ensure(project_key, project_index(project_id), created_or_updated)
        initial_context = build_okf_document(
            document_type="context",
            title="Project context",
            description="Current global state of the project.",
            sources=[source_key],
            timestamp=datetime.now(UTC),
        )
        self._validate_wiki_document(initial_context, tenant_id, client_id, project_id, required_type="context")
        self._ensure(context_key, initial_context, created_or_updated)
        self._ensure(log_key, log_header(tenant_id), created_or_updated)
        self._update(tenant_key, lambda text: append_link(text, client_id, f"{client_id}/index.md"), created_or_updated)
        self._update(client_key, lambda text: append_link(text, project_id, f"{project_id}/index.md"), created_or_updated)
        self._update(project_key, lambda text: append_link(text, title, f"/{source_key}"), created_or_updated)
        if source_action != "unchanged":
            timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            operation = "ingest" if source_action == "created" else "provenance"
            event = f"- `{timestamp}` {operation}: `/{source_key}` (client={client_id}, project={project_id}; wiki={', '.join(f'`/{key}`' for key in created_or_updated if key != source_key)})"
            self._update(log_key, lambda text: text.rstrip() + "\n" + event + "\n", created_or_updated)
        return {
            "source_key": source_key,
            "raw_key": raw_keys[0] if raw_keys else None,
            "provenance_status": "complete" if raw_keys else "analysis_only",
            "ingest_status": source_action,
            "updated_keys": created_or_updated,
        }

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

    def logs(self, tenant_id: str, limit: int | None) -> dict[str, object]:
        key = f"wiki/{tenant_id}/log.md"
        text = self.storage.get_text(key).text
        entries = [line for line in text.splitlines() if line.startswith("- `")]
        ordered = entries if re.search(r"^## \d{4}-\d{2}-\d{2}$", text, re.MULTILINE) else list(reversed(entries))
        return {"key": key, "entries": ordered[:limit]}

    def dashboard_summary(self, tenant_id: str) -> dict[str, object]:
        wiki_keys, source_keys = self.storage.list_keys(f"wiki/{tenant_id}/"), self.storage.list_keys(f"sources/{tenant_id}/")
        wiki_keys = self._canonical_scope_keys(wiki_keys)
        source_keys = self._canonical_scope_keys(source_keys)
        events = self._activity_entries(tenant_id)
        knowledge_pages = [key for key in wiki_keys if key.endswith(".md") and (key.endswith("/context.md") or "/decisions/" in key or "/risks/" in key)]
        return {"tenant_id": tenant_id, "client_count": len(self._client_ids(tenant_id, wiki_keys)), "project_count": len(self._project_index_keys(tenant_id, wiki_keys)), "source_count": len([key for key in source_keys if key.endswith(".md")]), "wiki_page_count": len([key for key in source_keys if key.endswith(".md")]) + len(knowledge_pages), "last_activity_at": events[0]["timestamp"] if events else None}

    def dashboard_clients(self, tenant_id: str, limit: int, offset: int) -> dict[str, object]:
        wiki_keys, source_keys, events = self.storage.list_keys(f"wiki/{tenant_id}/"), self.storage.list_keys(f"sources/{tenant_id}/"), self._activity_entries(tenant_id)
        source_keys = self._canonical_scope_keys(source_keys)
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
            project_prefix = f"wiki/{tenant_id}/{client_id}/{project_id}/"
            knowledge_count = len([wiki_key for wiki_key in wiki_keys if wiki_key.startswith(project_prefix) and wiki_key.endswith(".md") and (wiki_key.endswith("/context.md") or "/decisions/" in wiki_key or "/risks/" in wiki_key)])
            projects.append({"project_id": project_id, "source_count": source_count, "wiki_page_count": source_count + knowledge_count, "last_activity_at": self._last_activity(events, client_id, project_id), "key": key})
        projects.sort(key=lambda item: item["project_id"])
        return {"total": len(projects), "items": projects[offset:offset + limit], "limit": limit, "offset": offset}

    def activity(self, tenant_id: str, limit: int) -> dict[str, object]:
        return {"entries": self._activity_entries(tenant_id)[:limit]}

    def list_documents(
        self, tenant_id: str, client_id: str | None, project_id: str | None, *,
        document_types: set[str] | None = None, limit: int | None = None, offset: int = 0,
    ) -> dict[str, object]:
        if not client_id or not project_id:
            raise ValueError("client_id and project_id are required for user-facing project files")
        source_prefix = f"sources/{tenant_id}/{client_id}/{project_id}/"
        items = []
        for key in sorted(self.storage.list_keys(source_prefix), reverse=True):
            if key.endswith(".md"):
                obj = self.storage.get_text(key)
                metadata, body = parse_front_matter(obj.text)
                stem = key.rsplit("/", 1)[-1].removesuffix(".md")
                raw_files = self._transcript_names(tenant_id, client_id, project_id, metadata.get("raw_sources"))
                items.append({
                    "document": f"analysis:{stem}", "title": metadata.get("title") or self._analysis_title(stem),
                    "key": key, "updated_at": self._updated_at(obj), "type": "meeting",
                    "description": metadata.get("description") or self._markdown_snippet(body),
                    "source_kind": metadata.get("source_kind", "meeting_analysis"),
                    "provenance_status": metadata.get("provenance_status", "analysis_only"),
                    "raw_available": bool(raw_files),
                    "raw_files": raw_files,
                })
        context_key = f"wiki/{tenant_id}/{client_id}/{project_id}/context.md"
        try:
            context = self.storage.get_text(context_key)
            context_metadata, _ = parse_front_matter(context.text)
            items.append({
                "document": "context", "title": context_metadata.get("title") or "Project context",
                "description": context_metadata.get("description"), "key": context_key,
                "updated_at": self._updated_at(context), "type": "context",
            })
        except ObjectNotFound:
            pass
        project_prefix = f"wiki/{tenant_id}/{client_id}/{project_id}"
        for document_type, folder in (("decision", "decisions"), ("risk", "risks")):
            for key in sorted(self.storage.list_keys(f"{project_prefix}/{folder}/"), reverse=True):
                if not key.endswith(".md"):
                    continue
                obj = self.storage.get_text(key)
                metadata, _ = parse_front_matter(obj.text)
                stem = key.rsplit("/", 1)[-1].removesuffix(".md")
                items.append({
                    "document": f"{document_type}:{stem}", "title": metadata.get("title") or self._analysis_title(stem),
                    "description": metadata.get("description"), "key": key, "updated_at": self._updated_at(obj),
                    "type": document_type,
                })
        if not items:
            raise ObjectNotFound(source_prefix)
        selected = [item for item in items if not document_types or item["type"] in document_types]
        page = selected[offset:] if limit is None else selected[offset:offset + limit]
        return {"total": len(selected), "items": page, "limit": limit, "offset": offset}

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
        elif document.startswith("decision:") or document.startswith("risk:"):
            document_type, stem = document.split(":", 1)
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", stem):
                raise ValueError("invalid derived document")
            folder = "decisions" if document_type == "decision" else "risks"
            key, title, is_analysis = f"wiki/{tenant_id}/{client_id}/{project_id}/{folder}/{stem}.md", self._analysis_title(stem), False
        else:
            raise ValueError("document must be a project context, meeting analysis, decision, or risk")
        obj = self.storage.get_text(key)
        metadata, body = parse_front_matter(obj.text)
        response = {
            "document": document, "title": metadata.get("title") or title, "description": metadata.get("description"),
            "key": key, "content_markdown": body if is_analysis else obj.text, "body_markdown": body,
            "metadata": metadata, "type": "meeting" if is_analysis else metadata.get("type"),
            "content_type": "text/markdown", "updated_at": self._updated_at(obj),
        }
        if is_analysis:
            raw_files = self._transcript_names(tenant_id, client_id, project_id, metadata.get("raw_sources"))
            response.update({
                "source_kind": metadata.get("source_kind", "meeting_analysis"),
                "provenance_status": metadata.get("provenance_status", "analysis_only"),
                "raw_available": bool(raw_files),
                "raw_files": raw_files,
            })
        return response

    def read_transcript(self, tenant_id: str, client_id: str | None, project_id: str | None, document: str, file: str | None) -> dict[str, object]:
        if not client_id or not project_id:
            raise ValueError("client_id and project_id are required for user-facing project files")
        if not document.startswith("analysis:"):
            raise ValueError("raw transcript is only available for a meeting analysis")
        stem = document.removeprefix("analysis:")
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", stem):
            raise ValueError("invalid analysis document")
        source_key = f"sources/{tenant_id}/{client_id}/{project_id}/{stem}.md"
        metadata, _ = parse_front_matter(self.storage.get_text(source_key).text)
        transcripts = self._transcripts(tenant_id, client_id, project_id, metadata.get("raw_sources"))
        if not transcripts:
            return {
                "document": document, "provenance_status": "analysis_only", "raw_available": False,
                "file": None, "files": [], "content_text": None,
            }
        if file is None:
            if len(transcripts) != 1:
                raise ValueError("file is required when the meeting has more than one transcript")
            name, raw_key = transcripts[0]
        else:
            if not self._is_transcript_filename(file):
                raise ValueError("invalid transcript file")
            match = next((item for item in transcripts if item[0] == file), None)
            if match is None:
                raise ObjectNotFound(file)
            name, raw_key = match
        stored = self.storage.get_bytes(raw_key)
        try:
            text = stored.data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("transcript must be UTF-8") from exc
        return {
            "document": document,
            "provenance_status": metadata.get("provenance_status", "complete"),
            "raw_available": True,
            "file": name,
            "files": [item[0] for item in transcripts],
            "content_text": text,
            "content_type": stored.content_type or "text/plain; charset=utf-8",
        }

    def _transcript_names(self, tenant_id: str, client_id: str, project_id: str, raw_sources: object) -> list[str]:
        return [name for name, _key in self._transcripts(tenant_id, client_id, project_id, raw_sources)]

    def _transcripts(self, tenant_id: str, client_id: str, project_id: str, raw_sources: object) -> list[tuple[str, str]]:
        prefix = f"raw/{tenant_id}/{client_id}/{project_id}/"
        sources = raw_sources if isinstance(raw_sources, list) else []
        candidates: list[tuple[str, str]] = []
        for raw_key in sources:
            if not isinstance(raw_key, str) or not raw_key.startswith(prefix):
                continue
            relative = raw_key[len(prefix):]
            parts = relative.split("/")
            if not parts or any(part in {"", ".", ".."} or "\\" in part for part in parts):
                continue
            candidates.append((parts[-1], raw_key))
        transcripts: list[tuple[str, str]] = []
        seen: set[str] = set()
        for name, raw_key in candidates:
            public = name if sum(item[0] == name for item in candidates) == 1 else raw_key[len(prefix):]
            if public in seen:
                continue
            seen.add(public)
            transcripts.append((public, raw_key))
        return transcripts

    @staticmethod
    def _is_transcript_filename(name: str) -> bool:
        return bool(name) and "\\" not in name and ".." not in name.split("/") and all(part not in {"", ".", ".."} for part in name.split("/"))

    def update_context(self, tenant_id: str, client_id: str, project_id: str, content_markdown: str) -> dict[str, object]:
        key = f"wiki/{tenant_id}/{client_id}/{project_id}/context.md"
        self._validate_wiki_document(content_markdown, tenant_id, client_id, project_id, required_type="context")
        for _ in range(5):
            current = self.storage.get_text(key)
            try:
                self.storage.put_if_match(key, content_markdown, current.etag)
                saved = self.storage.get_text(key)
                metadata, body = parse_front_matter(saved.text)
                return {
                    "document": "context", "title": metadata.get("title") or "Project context",
                    "description": metadata.get("description"), "key": key, "content_markdown": saved.text,
                    "body_markdown": body, "metadata": metadata, "type": "context",
                    "content_type": "text/markdown", "updated_at": self._updated_at(saved),
                }
            except PreconditionFailed:
                continue
        raise StorageError("context update conflicted repeatedly")

    def apply_librarian(self, payload: dict[str, object]) -> dict[str, object]:
        tenant_id = str(payload["tenant_id"]); client_id = str(payload["client_id"]); project_id = str(payload["project_id"])
        source_key = str(payload["source_key"]); expected_etag = str(payload["expected_context_etag"])
        expected_source_prefix = f"sources/{tenant_id}/{client_id}/{project_id}/"
        if not source_key.startswith(expected_source_prefix) or not source_key.endswith(".md"):
            raise ValueError("source_key is outside the requested project")
        if str(payload.get("job_id", "")) != sha256(source_key.encode("utf-8")).hexdigest()[:32]:
            raise ValueError("job_id does not match source_key")
        self.storage.get_text(source_key)
        context_key = f"wiki/{tenant_id}/{client_id}/{project_id}/context.md"
        current_context = self.storage.get_text(context_key)
        if current_context.etag != expected_etag:
            raise ContextConflict(context_key)

        context = payload.get("context")
        if not isinstance(context, dict):
            raise ValueError("context draft is required")
        context_sources = context.get("sources")
        if not isinstance(context_sources, list) or source_key not in context_sources:
            raise ValueError("context sources must include the processed analysis")
        context_body = context.get("body")
        if not isinstance(context_body, str) or not all(re.search(rf"^##? {heading}\s*$", context_body, re.MULTILINE | re.IGNORECASE) for heading in ("Estado actual", "Hitos", "Pendientes")):
            raise ValueError("context body must contain Estado actual, Hitos, and Pendientes headings")
        context_markdown = build_okf_document(
            document_type="context",
            title=self._required_text(context, "title"),
            description=self._required_text(context, "description"),
            sources=[str(item) for item in context_sources],
            timestamp=self._parse_timestamp(self._required_text(context, "timestamp")),
            body=context_body,
        )
        self._validate_wiki_document(context_markdown, tenant_id, client_id, project_id, required_type="context")

        rendered_documents: list[tuple[str, str]] = []
        documents = payload.get("documents", [])
        if not isinstance(documents, list):
            raise ValueError("documents must be a list")
        for draft in documents:
            if not isinstance(draft, dict):
                raise ValueError("each document draft must be an object")
            document_type = draft.get("type")
            if document_type not in {"decision", "risk"}:
                raise ValueError("librarian documents must be decisions or risks")
            title = self._required_text(draft, "title")
            timestamp_text = self._required_text(draft, "timestamp")
            timestamp = self._parse_timestamp(timestamp_text)
            folder = "decisions" if document_type == "decision" else "risks"
            stable_id = sha256(f"{source_key}|{document_type}|{title}|{timestamp_text}".encode()).hexdigest()[:10]
            key = f"wiki/{tenant_id}/{client_id}/{project_id}/{folder}/{timestamp:%Y-%m-%d}-{slugify(title)}-{stable_id}.md"
            extra: dict[str, object] = {}
            for relation in ("supersedes", "related"):
                values = draft.get(relation, [])
                if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                    raise ValueError(f"{relation} must be a list of Wiki keys")
                relation_pattern = re.compile(rf"^wiki/{re.escape(tenant_id)}/{re.escape(client_id)}/{re.escape(project_id)}/(?:decisions|risks)/[a-zA-Z0-9_-]+\.md$")
                if any(not relation_pattern.fullmatch(value) for value in values):
                    raise ValueError(f"{relation} must remain inside the project")
                if values:
                    extra[relation] = values
            markdown = build_okf_document(
                document_type=str(document_type), title=title,
                description=self._required_text(draft, "description"), sources=[source_key],
                timestamp=timestamp, body=self._required_text(draft, "body"), extra=extra,
            )
            self._validate_wiki_document(markdown, tenant_id, client_id, project_id, required_type=str(document_type))
            rendered_documents.append((key, markdown))

        output_keys: list[str] = []
        for key, markdown in rendered_documents:
            self._put_if_absent_identical(key, markdown)
            output_keys.append(key)
        try:
            self.storage.put_if_match(context_key, context_markdown, expected_etag)
        except PreconditionFailed as exc:
            raise ContextConflict(context_key) from exc
        output_keys.insert(0, context_key)
        project_key = f"wiki/{tenant_id}/{client_id}/{project_id}/index.md"
        self._replace(project_key, self._render_project_index(tenant_id, client_id, project_id))
        output_keys.append(project_key)
        self._append_librarian_log(tenant_id, client_id, project_id, source_key, output_keys)
        return {"job_id": payload.get("job_id"), "output_keys": output_keys}

    def maintain_tenant(self, tenant_id: str) -> dict[str, object]:
        wiki_keys = self.storage.list_keys(f"wiki/{tenant_id}/")
        changed: list[str] = []
        log_key = f"wiki/{tenant_id}/log.md"
        try:
            log = self.storage.get_text(log_key)
            normalized = self._normalize_log(tenant_id, log.text)
            if normalized != log.text:
                self._replace(log_key, normalized); changed.append(log_key)
        except ObjectNotFound:
            pass
        project_keys = self._project_index_keys(tenant_id, wiki_keys)
        for key in project_keys:
            parts = key.split("/")
            rendered = self._render_project_index(tenant_id, parts[2], parts[3])
            if self._replace_if_changed(key, rendered): changed.append(key)
        clients = self._client_ids(tenant_id, wiki_keys)
        for client_id in clients:
            key = f"wiki/{tenant_id}/{client_id}/index.md"
            if self._replace_if_changed(key, self._render_client_index(tenant_id, client_id)): changed.append(key)
        tenant_key = f"wiki/{tenant_id}/index.md"
        if self._replace_if_changed(tenant_key, self._render_tenant_index(tenant_id)): changed.append(tenant_key)
        return {"tenant_id": tenant_id, "updated_keys": changed}

    @staticmethod
    def _analysis_title(stem: str) -> str:
        return stem.replace("-", " ").title()

    @staticmethod
    def _updated_at(obj) -> str | None:
        return obj.last_modified.isoformat().replace("+00:00", "Z") if obj.last_modified else None

    @staticmethod
    def _canonical_scope_keys(keys: list[str]) -> list[str]:
        result = []
        for key in keys:
            parts = key.split("/")
            if len(parts) >= 4 and parts[2] != normalize_scope_identifier(parts[2], "client_id"):
                continue
            if len(parts) >= 5 and parts[3] != normalize_scope_identifier(parts[3], "project_id"):
                continue
            result.append(key)
        return result

    @staticmethod
    def _client_ids(tenant_id: str, keys: list[str]) -> list[str]:
        prefix = f"wiki/{tenant_id}/"
        return sorted({key.split("/")[2] for key in keys if key.startswith(prefix) and len(key.split("/")) == 4 and key.endswith("/index.md") and key.split("/")[2] == normalize_scope_identifier(key.split("/")[2], "client_id")})

    @staticmethod
    def _project_index_keys(tenant_id: str, keys: list[str], client_id: str | None = None) -> list[str]:
        prefix = f"wiki/{tenant_id}/{client_id}/" if client_id else f"wiki/{tenant_id}/"
        return [key for key in keys if key.startswith(prefix) and len(key.split("/")) == 5 and key.endswith("/index.md") and key.split("/")[2] == normalize_scope_identifier(key.split("/")[2], "client_id") and key.split("/")[3] == normalize_scope_identifier(key.split("/")[3], "project_id")]

    def _activity_entries(self, tenant_id: str) -> list[dict[str, str]]:
        try:
            entries = self.logs(tenant_id, None)["entries"]
        except ObjectNotFound:
            return []
        pattern = re.compile(r"^- `([^`]+)` (ingest|provenance|librarian): `/?([^`]+)` \(client=([^,]+), project=([^;]+);")
        activity = []
        for entry in entries:
            match = pattern.match(entry)
            if not match:
                continue
            try:
                timestamp = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
                client_id = normalize_scope_identifier(match.group(4), "client_id")
                project_id = normalize_scope_identifier(match.group(5), "project_id")
            except ValueError:
                continue
            if timestamp.tzinfo is None:
                continue
            activity.append({
                "timestamp": timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "event": match.group(2), "source_key": match.group(3),
                "client_id": client_id, "project_id": project_id, "raw": entry,
            })
        activity.sort(key=lambda item: datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00")), reverse=True)
        return activity

    @staticmethod
    def _last_activity(entries: list[dict[str, str]], client_id: str, project_id: str | None = None) -> str | None:
        return next((entry["timestamp"] for entry in entries if entry["client_id"] == client_id and (project_id is None or entry["project_id"] == project_id)), None)

    @staticmethod
    def _safe_raw_filename(filename: str) -> str:
        basename = re.split(r"[\\/]", filename)[-1]
        if not basename or "." not in basename:
            raise ValueError("raw_file must have a filename with an extension")
        stem, extension = basename.rsplit(".", 1)
        if extension.lower() not in {"md", "txt"}:
            raise ValueError("raw_file must be a UTF-8 .md or .txt file")
        return f"{slugify(stem)}.{extension.lower()}"

    @staticmethod
    def _same_source_analysis(existing: str, expected_analysis_only: str) -> bool:
        existing_metadata, existing_body = parse_front_matter(existing)
        expected_metadata, expected_body = parse_front_matter(expected_analysis_only)
        managed_fields = {
            "source_kind", "analysis_schema_version", "raw_sources", "raw_sha256", "provenance_status",
        }
        existing_base = {key: value for key, value in existing_metadata.items() if key not in managed_fields}
        expected_base = {key: value for key, value in expected_metadata.items() if key not in managed_fields}
        return existing_base == expected_base and existing_body == expected_body

    @staticmethod
    def _required_text(value: dict[str, object], field: str) -> str:
        item = value.get(field)
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field} must be a non-empty string")
        return item

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        if not value.endswith("Z"):
            raise ValueError("timestamp must be RFC 3339 UTC ending in Z")
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError as exc:
            raise ValueError("timestamp must be RFC 3339 UTC ending in Z") from exc
        return parsed

    def _put_if_absent_identical(self, key: str, text: str) -> None:
        try:
            self.storage.put_if_absent(key, text)
        except PreconditionFailed:
            if self.storage.get_text(key).text != text:
                raise ValueError(f"existing derived document differs: {key}")

    def _replace(self, key: str, text: str) -> None:
        for _ in range(5):
            current = self.storage.get_text(key)
            if current.text == text:
                return
            try:
                self.storage.put_if_match(key, text, current.etag)
                return
            except PreconditionFailed:
                continue
        raise StorageError(f"too much concurrent activity updating {key}")

    def _replace_if_changed(self, key: str, text: str) -> bool:
        try:
            current = self.storage.get_text(key)
        except ObjectNotFound:
            try:
                self.storage.put_if_absent(key, text)
                return True
            except PreconditionFailed:
                current = self.storage.get_text(key)
        if current.text == text:
            return False
        self._replace(key, text)
        return True

    def _render_project_index(self, tenant_id: str, client_id: str, project_id: str) -> str:
        project_prefix = f"wiki/{tenant_id}/{client_id}/{project_id}"
        lines = [f"# Wiki: project {project_id}", "", "## Context", ""]
        context_key = f"{project_prefix}/context.md"
        try:
            context = self.storage.get_text(context_key)
            metadata, _ = parse_front_matter(context.text)
            lines.append(f"- [Context](context.md) — {metadata.get('description', 'Current project context')}")
        except ObjectNotFound:
            pass
        lines.extend(["", "## Reuniones analizadas", ""])
        source_prefix = f"sources/{tenant_id}/{client_id}/{project_id}/"
        for key in sorted(self.storage.list_keys(source_prefix), reverse=True):
            if not key.endswith(".md"): continue
            metadata, _ = parse_front_matter(self.storage.get_text(key).text)
            title = metadata.get("title") or self._analysis_title(key.rsplit("/", 1)[-1].removesuffix(".md"))
            date = metadata.get("date_time", "")
            description = f"Reunión analizada {str(date)[:10]}".rstrip()
            lines.append(f"- [{title}](/{key}) — {description}")
        for heading, folder in (("Decisiones", "decisions"), ("Riesgos", "risks")):
            lines.extend(["", f"## {heading}", ""])
            for key in sorted(self.storage.list_keys(f"{project_prefix}/{folder}/"), reverse=True):
                if not key.endswith(".md"): continue
                metadata, _ = parse_front_matter(self.storage.get_text(key).text)
                relative = key.removeprefix(project_prefix + "/")
                lines.append(f"- [{metadata.get('title', self._analysis_title(relative))}]({relative}) — {metadata.get('description', '')}".rstrip())
        return "\n".join(lines).rstrip() + "\n"

    def _render_client_index(self, tenant_id: str, client_id: str) -> str:
        keys = self.storage.list_keys(f"wiki/{tenant_id}/{client_id}/")
        lines = [f"# Wiki: client {client_id}", "", "## Projects", ""]
        for key in sorted(self._project_index_keys(tenant_id, keys, client_id)):
            project_id = key.split("/")[3]
            description = "Project knowledge"
            try:
                context = self.storage.get_text(f"wiki/{tenant_id}/{client_id}/{project_id}/context.md")
                description = str(parse_front_matter(context.text)[0].get("description") or description)
            except ObjectNotFound:
                pass
            lines.append(f"- [{project_id}]({project_id}/index.md) — {description}")
        return "\n".join(lines).rstrip() + "\n"

    def _render_tenant_index(self, tenant_id: str) -> str:
        keys = self.storage.list_keys(f"wiki/{tenant_id}/")
        lines = [f"# Wiki: tenant {tenant_id}", "", "## Clients", ""]
        for client_id in self._client_ids(tenant_id, keys):
            projects = self._project_index_keys(tenant_id, keys, client_id)
            lines.append(f"- [{client_id}]({client_id}/index.md) — {len(projects)} project(s)")
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _normalize_log(tenant_id: str, text: str) -> str:
        entries = {line for line in text.splitlines() if line.startswith("- `")}
        grouped: dict[str, list[tuple[str, str]]] = {}
        for line in entries:
            match = re.match(r"^- `([^`]+)`", line)
            if not match: continue
            timestamp = match.group(1)
            try:
                parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError:
                continue
            grouped.setdefault(parsed.date().isoformat(), []).append((timestamp, line))
        lines = [f"# Activity log: tenant {tenant_id}", ""]
        for date in sorted(grouped, reverse=True):
            lines.extend([f"## {date}", ""])
            lines.extend(line for _, line in sorted(grouped[date], key=lambda item: item[0], reverse=True))
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _append_librarian_log(self, tenant_id: str, client_id: str, project_id: str, source_key: str, output_keys: list[str]) -> None:
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        event = f"- `{timestamp}` librarian: `/{source_key}` (client={client_id}, project={project_id}; outputs={', '.join(f'`/{key}`' for key in output_keys)})"
        log_key = f"wiki/{tenant_id}/log.md"
        self._update(log_key, lambda text: text if event in text else text.rstrip() + "\n" + event + "\n", [])

    def _validate_wiki_document(self, markdown: str, tenant_id: str, client_id: str, project_id: str, *, required_type: str | None = None) -> None:
        metadata = validate_okf_document(
            markdown,
            tenant_id=tenant_id,
            client_id=client_id,
            project_id=project_id,
            required_type=required_type,
        )
        for source_key in metadata["sources"]:
            try:
                self.storage.get_text(source_key)
            except ObjectNotFound as exc:
                raise ValueError(f"OKF source does not exist: {source_key}") from exc

    @staticmethod
    def _markdown_snippet(markdown: str, limit: int = 180) -> str:
        for paragraph in re.split(r"\n\s*\n", markdown):
            lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
            if not lines or all(line.startswith(("#", "|", "-", "*", "[")) for line in lines):
                continue
            text = " ".join(lines)
            text = re.sub(r"[`*_>#]", "", text)
            text = re.sub(r"\[([^]]+)]\([^)]+\)", r"\1", text)
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
        return "Reunión analizada del proyecto."

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

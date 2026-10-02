"""Personal work inbox: commitments and notes stored in D1.

Meeting commitments are visible to every tenant member. Manual and MCP
commitments, and every note, stay with the person who created them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo
import re
import unicodedata

DEFAULT_TIMEZONE = "America/Santiago"
WEEK_RE = re.compile(r"^\d{4}-W(0[1-9]|[1-4]\d|5[0-3])$")
DUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
REQUEST_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
NOTE_BODY_LIMIT = 20_000

PUBLIC_COMMITMENT_FIELDS = (
    "id", "tenant_id", "client_id", "project_id", "title", "detail", "status",
    "week", "due_on", "origin", "source_key", "evidence", "suggested_status",
    "assignee_label", "created_at", "updated_at", "completed_at",
)


class RequestConflict(Exception):
    """The same client_request_id was reused with a different body."""


class NotAllowed(Exception):
    """The caller cannot change another member's manual commitment."""


class NotFound(Exception):
    pass


class WorkStore(Protocol):
    def timezone_for(self, google_sub: str) -> str: ...
    def set_timezone(self, google_sub: str, timezone: str) -> dict[str, Any]: ...
    def upsert_meeting_commitments(
        self, *, tenant_id: str, client_id: str, project_id: str, source_key: str,
        meeting_at: datetime, items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]: ...
    def create_commitment(self, *, owner_sub: str, tenant_id: str, fields: dict[str, Any]) -> dict[str, Any]: ...
    def update_commitment(self, *, actor_sub: str, tenant_id: str, commitment_id: str, changes: dict[str, Any]) -> dict[str, Any]: ...
    def visible_commitments(self, tenant_id: str, owner_sub: str, client_id: str | None) -> list[dict[str, Any]]: ...
    def create_note(self, *, author_sub: str, tenant_id: str, fields: dict[str, Any]) -> dict[str, Any]: ...
    def list_notes(
        self, *, author_sub: str, tenant_id: str, client_id: str | None, project_id: str | None,
        query: str | None, limit: int, offset: int,
    ) -> dict[str, Any]: ...
    def get_note(self, *, author_sub: str, tenant_id: str, note_id: str) -> dict[str, Any]: ...
    def update_note(self, *, author_sub: str, tenant_id: str, note_id: str, changes: dict[str, Any]) -> dict[str, Any]: ...
    def delete_note(self, *, author_sub: str, tenant_id: str, note_id: str) -> None: ...


def now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def validate_timezone(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timezone is required")
    name = value.strip()
    try:
        ZoneInfo(name)
    except Exception as exc:
        raise ValueError("timezone must be an IANA timezone name") from exc
    return name


def week_of(moment: datetime, timezone: str = DEFAULT_TIMEZONE) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    local = moment.astimezone(ZoneInfo(timezone))
    iso = local.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def validate_week(value: str) -> str:
    if not isinstance(value, str) or not WEEK_RE.fullmatch(value):
        raise ValueError("week must be an ISO week like 2026-W40")
    return value


def validate_due(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not DUE_RE.fullmatch(value):
        raise ValueError("due_on must be YYYY-MM-DD")
    datetime.strptime(value, "%Y-%m-%d")
    return value


def normalize_title(title: str) -> str:
    folded = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", folded).strip()


def external_key_for(source_key: str, title: str) -> str:
    normalized = normalize_title(title)
    if not normalized:
        raise ValueError("title must contain at least one letter or number")
    return sha256(f"{source_key}\n{normalized}".encode()).hexdigest()[:32]


def commitment_fingerprint(fields: dict[str, Any]) -> str:
    parts = [
        fields.get("title") or "", fields.get("detail") or "", fields.get("client_id") or "",
        fields.get("project_id") or "", fields.get("week") or "", fields.get("origin") or "manual",
    ]
    return sha256("\n".join(str(part) for part in parts).encode()).hexdigest()


def note_fingerprint(fields: dict[str, Any]) -> str:
    source = fields.get("source") or fields.get("origin") or "manual"
    parts = [fields.get("title") or "", fields.get("body") or "", fields.get("client_id") or "", fields.get("project_id") or "", source]
    return sha256("\n".join(str(part) for part in parts).encode()).hexdigest()


def public_commitment(row: dict[str, Any], viewer_sub: str | None = None) -> dict[str, Any]:
    item = {field: row.get(field) for field in PUBLIC_COMMITMENT_FIELDS}
    if viewer_sub is not None:
        item["mine"] = row.get("owner_sub") == viewer_sub
    return item


def _completed_moment(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def in_week(completed_at: str | None, week: str, timezone: str) -> bool:
    moment = _completed_moment(completed_at)
    return bool(moment and week_of(moment, timezone) == week)


def build_week_view(rows: list[dict[str, Any]], *, timezone: str, week: str) -> dict[str, Any]:
    open_items = sorted((row for row in rows if row["status"] == "open" and row["week"] == week), key=lambda row: row["title"])
    overdue = sorted((row for row in rows if row["status"] == "open" and row["week"] < week), key=lambda row: (row["week"], row["title"]))
    done_items = sorted(
        (row for row in rows if row["status"] == "done" and in_week(row.get("completed_at"), week, timezone)),
        key=lambda row: row.get("completed_at") or "",
        reverse=True,
    )
    return {
        "week": week,
        "timezone": timezone,
        "open_count": len(open_items),
        "overdue_count": len(overdue),
        "done_this_week": len(done_items),
        "all_clear": not open_items and not overdue,
        "open_items": open_items,
        "overdue": overdue,
        "done_items": done_items,
    }


def build_pending_view(
    rows: list[dict[str, Any]],
    *,
    tenant_id: str,
    timezone: str,
    now: datetime,
    client_id: str | None,
    decision_digest,
    latest_meeting,
) -> dict[str, Any]:
    current_week = week_of(now, timezone)
    selected = [row for row in rows if client_id is None or row.get("client_id") == client_id]
    done_items = [
        row for row in selected
        if row["status"] == "done" and in_week(row.get("completed_at"), current_week, timezone)
    ]
    grouped: dict[tuple[str | None, str | None], list[dict[str, Any]]] = {}
    for row in selected:
        if row["status"] == "dropped":
            continue
        include = row["status"] == "open" or row in done_items
        if not include:
            continue
        grouped.setdefault((row.get("client_id"), row.get("project_id")), []).append(row)

    clients: dict[str | None, dict[str, Any]] = {}
    for (group_client, project_id), items in sorted(grouped.items(), key=lambda item: (item[0][0] or "", item[0][1] or "")):
        open_items = sorted((item for item in items if item["status"] == "open"), key=lambda item: (item["week"], item["title"]))
        done_count = sum(1 for item in selected if item.get("client_id") == group_client and item.get("project_id") == project_id and item["status"] == "done")
        digest = {"decision_count": 0, "recent_decisions": []}
        last_updated = None
        if group_client and project_id:
            digest = decision_digest(group_client, project_id)
            last_updated = latest_meeting(group_client, project_id)
        project = {
            "project_id": project_id,
            "open_count": len(open_items),
            "done_count": done_count,
            "last_updated_at": last_updated,
            "decision_count": digest["decision_count"],
            "recent_decisions": digest["recent_decisions"],
            "open_items": open_items,
        }
        bucket = clients.setdefault(group_client, {"client_id": group_client, "projects": []})
        bucket["projects"].append(project)

    client_rows = []
    for bucket in clients.values():
        bucket["open_count"] = sum(project["open_count"] for project in bucket["projects"])
        bucket["project_count"] = sum(1 for project in bucket["projects"] if project["project_id"] and project["open_count"] > 0)
        client_rows.append(bucket)
    open_count = sum(bucket["open_count"] for bucket in client_rows)
    project_count = sum(bucket["project_count"] for bucket in client_rows)
    payload: dict[str, Any] = {
        "tenant_id": tenant_id,
        "timezone": timezone,
        "week": current_week,
        "open_count": open_count,
        "project_count": project_count,
        "client_count": sum(1 for bucket in client_rows if bucket["open_count"] > 0),
        "all_clear": open_count == 0,
        "done_this_week": len(done_items),
        "done_items": sorted(done_items, key=lambda item: item.get("completed_at") or "", reverse=True),
        "clients": client_rows,
    }
    if client_id is not None:
        payload["client_id"] = client_id
    return payload


def _like(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class MemoryWorkStore:
    def __init__(self) -> None:
        self.settings: dict[str, str] = {}
        self.commitments: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.notes: dict[str, dict[str, Any]] = {}
        self._requests: dict[tuple[str, str], str] = {}

    def timezone_for(self, google_sub: str) -> str:
        return self.settings.get(google_sub, DEFAULT_TIMEZONE)

    def set_timezone(self, google_sub: str, timezone: str) -> dict[str, Any]:
        name = validate_timezone(timezone)
        self.settings[google_sub] = name
        return {"timezone": name}

    def upsert_meeting_commitments(
        self, *, tenant_id: str, client_id: str, project_id: str, source_key: str,
        meeting_at: datetime, items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        saved = []
        for item in items:
            external = item["external_key"]
            existing = next((row for row in self.commitments.values() if row["tenant_id"] == tenant_id and row.get("external_key") == external), None)
            if existing:
                if item.get("suggested_status") == "done" and existing["status"] == "open" and existing.get("suggested_status") != "done":
                    existing["suggested_status"] = "done"
                    existing["updated_at"] = now_iso()
                saved.append(dict(existing))
                continue
            saved.append(self._insert_commitment({
                "tenant_id": tenant_id,
                "client_id": client_id,
                "project_id": project_id,
                "owner_sub": None,
                "title": item["title"],
                "detail": item.get("detail") or "",
                "status": "open",
                "week": item.get("week") or week_of(meeting_at, DEFAULT_TIMEZONE),
                "due_on": item.get("due_on"),
                "origin": "meeting",
                "source_key": source_key,
                "evidence": item.get("evidence") or "",
                "external_key": external,
                "client_request_id": None,
                "suggested_status": item.get("suggested_status"),
                "assignee_label": None,
            }, actor_sub=None))
        return saved

    def create_commitment(self, *, owner_sub: str, tenant_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        week = fields.get("week") or week_of(datetime.now(UTC), self.timezone_for(owner_sub))
        origin = fields.get("origin") or "manual"
        request_id = fields.get("client_request_id")
        fingerprint = commitment_fingerprint({**fields, "week": week, "origin": origin})
        existing = self._reuse(owner_sub, request_id, fingerprint, lambda: next((row for row in self.commitments.values() if row.get("owner_sub") == owner_sub and row.get("client_request_id") == request_id), None))
        if existing:
            return dict(existing)
        return self._insert_commitment({
            "tenant_id": tenant_id,
            "client_id": fields.get("client_id"),
            "project_id": fields.get("project_id"),
            "owner_sub": owner_sub,
            "title": fields["title"],
            "detail": fields.get("detail") or "",
            "status": "open",
            "week": week,
            "due_on": fields.get("due_on"),
            "origin": origin,
            "source_key": None,
            "evidence": "",
            "external_key": None,
            "client_request_id": request_id,
            "suggested_status": None,
            "assignee_label": fields.get("assignee_label"),
        }, actor_sub=owner_sub)

    def update_commitment(self, *, actor_sub: str, tenant_id: str, commitment_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        row = self.commitments.get(commitment_id)
        if row is None or row["tenant_id"] != tenant_id:
            raise NotFound(commitment_id)
        if row["origin"] != "meeting" and row.get("owner_sub") != actor_sub:
            raise NotAllowed(commitment_id)
        previous_status, previous_week = row["status"], row["week"]
        for field in ("title", "detail", "client_id", "project_id", "week", "due_on", "status"):
            if field in changes:
                row[field] = changes[field]
        if row["status"] != previous_status:
            row["completed_at"] = now_iso() if row["status"] in {"done", "dropped"} else None
        changed = row["status"] != previous_status or row["week"] != previous_week
        if changed:
            row["updated_at"] = now_iso()
            if row["status"] == "done":
                row["suggested_status"] = None
            self._event(row["id"], actor_sub, previous_status, row["status"], previous_week, row["week"])
        return dict(row)

    def visible_commitments(self, tenant_id: str, owner_sub: str, client_id: str | None) -> list[dict[str, Any]]:
        rows = []
        for row in self.commitments.values():
            if row["tenant_id"] != tenant_id:
                continue
            if row["origin"] != "meeting" and row.get("owner_sub") != owner_sub:
                continue
            if client_id is not None and row.get("client_id") != client_id:
                continue
            rows.append(dict(row))
        return rows

    def create_note(self, *, author_sub: str, tenant_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        source = fields.get("source") or "manual"
        request_id = fields.get("client_request_id")
        existing = self._reuse(author_sub, request_id, note_fingerprint({**fields, "source": source}), lambda: next((note for note in self.notes.values() if note.get("author_sub") == author_sub and note.get("client_request_id") == request_id), None))
        if existing:
            return dict(existing)
        note_id = uuid4().hex
        timestamp = now_iso()
        note = {
            "id": note_id, "tenant_id": tenant_id, "author_sub": author_sub,
            "title": fields.get("title"), "body": fields["body"],
            "client_id": fields.get("client_id"), "project_id": fields.get("project_id"),
            "pinned": bool(fields.get("pinned")), "source": source,
            "client_request_id": request_id, "created_at": timestamp, "updated_at": timestamp,
        }
        self.notes[note_id] = note
        return dict(note)

    def list_notes(
        self, *, author_sub: str, tenant_id: str, client_id: str | None, project_id: str | None,
        query: str | None, limit: int, offset: int,
    ) -> dict[str, Any]:
        rows = [dict(note) for note in self.notes.values() if note["author_sub"] == author_sub and note["tenant_id"] == tenant_id]
        if client_id is not None:
            rows = [note for note in rows if note.get("client_id") == client_id]
        if project_id is not None:
            rows = [note for note in rows if note.get("project_id") == project_id]
        if query:
            needle = query.casefold()
            rows = [note for note in rows if needle in (note.get("title") or "").casefold() or needle in note["body"].casefold()]
        rows.sort(key=lambda note: (note["pinned"], note["updated_at"]), reverse=True)
        return {"total": len(rows), "items": rows[offset:offset + limit], "limit": limit, "offset": offset}

    def get_note(self, *, author_sub: str, tenant_id: str, note_id: str) -> dict[str, Any]:
        note = self.notes.get(note_id)
        if note is None or note["tenant_id"] != tenant_id or note["author_sub"] != author_sub:
            raise NotFound(note_id)
        return dict(note)

    def update_note(self, *, author_sub: str, tenant_id: str, note_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        note = self.notes.get(note_id)
        if note is None or note["tenant_id"] != tenant_id or note["author_sub"] != author_sub:
            raise NotFound(note_id)
        for field in ("title", "body", "client_id", "project_id", "pinned"):
            if field in changes:
                note[field] = changes[field]
        note["updated_at"] = now_iso()
        return dict(note)

    def delete_note(self, *, author_sub: str, tenant_id: str, note_id: str) -> None:
        note = self.notes.get(note_id)
        if note is None or note["tenant_id"] != tenant_id or note["author_sub"] != author_sub:
            raise NotFound(note_id)
        del self.notes[note_id]

    def _insert_commitment(self, fields: dict[str, Any], *, actor_sub: str | None) -> dict[str, Any]:
        commitment_id = uuid4().hex
        timestamp = now_iso()
        row = {"id": commitment_id, "visibility": "private", "created_at": timestamp, "updated_at": timestamp, "completed_at": None, **fields}
        self.commitments[commitment_id] = row
        self._event(commitment_id, actor_sub, None, "open", None, row["week"])
        return dict(row)

    def _event(self, commitment_id: str, actor_sub: str | None, from_status: str | None, to_status: str, from_week: str | None, to_week: str) -> None:
        if from_status == to_status and from_week == to_week:
            return
        self.events.append({
            "id": uuid4().hex, "commitment_id": commitment_id, "actor_sub": actor_sub,
            "from_status": from_status, "to_status": to_status, "from_week": from_week, "to_week": to_week,
            "created_at": now_iso(),
        })

    def _reuse(self, owner_sub: str, request_id: str | None, fingerprint: str, finder):
        if not request_id:
            return None
        if not REQUEST_RE.fullmatch(request_id):
            raise ValueError("client_request_id must be 8 to 80 letters, numbers, underscores or hyphens")
        key = (owner_sub, request_id)
        previous = self._requests.get(key)
        if previous is None:
            self._requests[key] = fingerprint
            return None
        if previous != fingerprint:
            raise RequestConflict(request_id)
        return finder()


class D1WorkStore:
    def __init__(self, database) -> None:
        self.db = database

    def timezone_for(self, google_sub: str) -> str:
        rows = self.db.query("SELECT timezone FROM user_settings WHERE google_sub=?", [google_sub])
        return rows[0]["timezone"] if rows else DEFAULT_TIMEZONE

    def set_timezone(self, google_sub: str, timezone: str) -> dict[str, Any]:
        name = validate_timezone(timezone)
        self.db.query(
            "INSERT INTO user_settings(google_sub,timezone,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(google_sub) DO UPDATE SET timezone=excluded.timezone, updated_at=excluded.updated_at",
            [google_sub, name, now_iso()],
        )
        return {"timezone": name}

    def upsert_meeting_commitments(
        self, *, tenant_id: str, client_id: str, project_id: str, source_key: str,
        meeting_at: datetime, items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        saved = []
        for item in items:
            rows = self.db.query("SELECT * FROM commitments WHERE tenant_id=? AND external_key=?", [tenant_id, item["external_key"]])
            if rows:
                row = rows[0]
                if item.get("suggested_status") == "done" and row["status"] == "open" and row.get("suggested_status") != "done":
                    timestamp = now_iso()
                    self.db.query("UPDATE commitments SET suggested_status='done', updated_at=? WHERE id=?", [timestamp, row["id"]])
                    row["suggested_status"] = "done"
                    row["updated_at"] = timestamp
                saved.append(row)
                continue
            saved.append(self._insert({
                "tenant_id": tenant_id, "client_id": client_id, "project_id": project_id, "owner_sub": None,
                "title": item["title"], "detail": item.get("detail") or "", "status": "open",
                "week": item.get("week") or week_of(meeting_at, DEFAULT_TIMEZONE), "due_on": item.get("due_on"),
                "origin": "meeting", "source_key": source_key, "evidence": item.get("evidence") or "",
                "external_key": item["external_key"], "client_request_id": None,
                "suggested_status": item.get("suggested_status"), "assignee_label": None,
            }, actor_sub=None))
        return saved

    def create_commitment(self, *, owner_sub: str, tenant_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        week = fields.get("week") or week_of(datetime.now(UTC), self.timezone_for(owner_sub))
        origin = fields.get("origin") or "manual"
        request_id = fields.get("client_request_id")
        if request_id:
            existing = self._existing_request(owner_sub, request_id, commitment_fingerprint({**fields, "week": week, "origin": origin}), "commitments")
            if existing:
                return existing
        return self._insert({
            "tenant_id": tenant_id, "client_id": fields.get("client_id"), "project_id": fields.get("project_id"),
            "owner_sub": owner_sub, "title": fields["title"], "detail": fields.get("detail") or "", "status": "open",
            "week": week,
            "due_on": fields.get("due_on"), "origin": origin, "source_key": None,
            "evidence": "", "external_key": None, "client_request_id": request_id, "suggested_status": None,
            "assignee_label": fields.get("assignee_label"),
        }, actor_sub=owner_sub)

    def update_commitment(self, *, actor_sub: str, tenant_id: str, commitment_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        rows = self.db.query("SELECT * FROM commitments WHERE id=? AND tenant_id=?", [commitment_id, tenant_id])
        if not rows:
            raise NotFound(commitment_id)
        row = rows[0]
        if row["origin"] != "meeting" and row.get("owner_sub") != actor_sub:
            raise NotAllowed(commitment_id)
        updated = dict(row)
        for field in ("title", "detail", "client_id", "project_id", "week", "due_on", "status"):
            if field in changes:
                updated[field] = changes[field]
        if updated["status"] != row["status"]:
            updated["completed_at"] = now_iso() if updated["status"] in {"done", "dropped"} else None
        if updated["status"] == "done":
            updated["suggested_status"] = None
        changed = any(updated.get(field) != row.get(field) for field in ("title", "detail", "client_id", "project_id", "week", "due_on", "status"))
        if not changed:
            return row
        timestamp = now_iso()
        self.db.query(
            "UPDATE commitments SET title=?, detail=?, client_id=?, project_id=?, week=?, due_on=?, status=?, "
            "completed_at=?, suggested_status=?, updated_at=? WHERE id=?",
            [updated["title"], updated["detail"], updated.get("client_id"), updated.get("project_id"), updated["week"],
             updated.get("due_on"), updated["status"], updated.get("completed_at"), updated.get("suggested_status"), timestamp, commitment_id],
        )
        if updated["status"] != row["status"] or updated["week"] != row["week"]:
            self._event(commitment_id, actor_sub, row["status"], updated["status"], row["week"], updated["week"])
        updated["updated_at"] = timestamp
        return updated

    def visible_commitments(self, tenant_id: str, owner_sub: str, client_id: str | None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM commitments WHERE tenant_id=? AND (origin='meeting' OR owner_sub=?)"
        params: list[Any] = [tenant_id, owner_sub]
        if client_id is not None:
            sql += " AND client_id=?"
            params.append(client_id)
        return self.db.query(sql, params)

    def create_note(self, *, author_sub: str, tenant_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        source = fields.get("source") or "manual"
        request_id = fields.get("client_request_id")
        if request_id:
            existing = self._existing_request(author_sub, request_id, note_fingerprint({**fields, "source": source}), "notes")
            if existing:
                return self._note(existing)
        note_id = uuid4().hex
        timestamp = now_iso()
        self.db.query(
            "INSERT INTO notes(id,tenant_id,author_sub,title,body,client_id,project_id,pinned,source,client_request_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            [note_id, tenant_id, author_sub, fields.get("title"), fields["body"], fields.get("client_id"), fields.get("project_id"),
             1 if fields.get("pinned") else 0, source, request_id, timestamp, timestamp],
        )
        return self.get_note(author_sub=author_sub, tenant_id=tenant_id, note_id=note_id)

    def list_notes(
        self, *, author_sub: str, tenant_id: str, client_id: str | None, project_id: str | None,
        query: str | None, limit: int, offset: int,
    ) -> dict[str, Any]:
        sql = "SELECT * FROM notes WHERE author_sub=? AND tenant_id=?"
        params: list[Any] = [author_sub, tenant_id]
        if client_id is not None:
            sql += " AND client_id=?"
            params.append(client_id)
        if project_id is not None:
            sql += " AND project_id=?"
            params.append(project_id)
        if query:
            pattern = _like(query)
            sql += " AND (IFNULL(title,'') LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')"
            params.extend([pattern, pattern])
        rows = [self._note(row) for row in self.db.query(sql, params)]
        rows.sort(key=lambda note: (note["pinned"], note["updated_at"]), reverse=True)
        return {"total": len(rows), "items": rows[offset:offset + limit], "limit": limit, "offset": offset}

    def get_note(self, *, author_sub: str, tenant_id: str, note_id: str) -> dict[str, Any]:
        rows = self.db.query("SELECT * FROM notes WHERE id=? AND tenant_id=? AND author_sub=?", [note_id, tenant_id, author_sub])
        if not rows:
            raise NotFound(note_id)
        return self._note(rows[0])

    def update_note(self, *, author_sub: str, tenant_id: str, note_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        current = self.get_note(author_sub=author_sub, tenant_id=tenant_id, note_id=note_id)
        for field in ("title", "body", "client_id", "project_id", "pinned"):
            if field in changes:
                current[field] = changes[field]
        timestamp = now_iso()
        self.db.query(
            "UPDATE notes SET title=?, body=?, client_id=?, project_id=?, pinned=?, updated_at=? WHERE id=?",
            [current.get("title"), current["body"], current.get("client_id"), current.get("project_id"), 1 if current.get("pinned") else 0, timestamp, note_id],
        )
        current["updated_at"] = timestamp
        return current

    def delete_note(self, *, author_sub: str, tenant_id: str, note_id: str) -> None:
        self.get_note(author_sub=author_sub, tenant_id=tenant_id, note_id=note_id)
        self.db.query("DELETE FROM notes WHERE id=? AND author_sub=?", [note_id, author_sub])

    def _insert(self, fields: dict[str, Any], *, actor_sub: str | None) -> dict[str, Any]:
        commitment_id = uuid4().hex
        timestamp = now_iso()
        self.db.query(
            "INSERT INTO commitments(id,tenant_id,client_id,project_id,owner_sub,title,detail,status,week,due_on,origin,source_key,evidence,external_key,client_request_id,suggested_status,assignee_label,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [commitment_id, fields["tenant_id"], fields.get("client_id"), fields.get("project_id"), fields.get("owner_sub"),
             fields["title"], fields.get("detail") or "", fields["status"], fields["week"], fields.get("due_on"), fields["origin"],
             fields.get("source_key"), fields.get("evidence") or "", fields.get("external_key"), fields.get("client_request_id"),
             fields.get("suggested_status"), fields.get("assignee_label"), timestamp, timestamp],
        )
        self._event(commitment_id, actor_sub, None, "open", None, fields["week"])
        rows = self.db.query("SELECT * FROM commitments WHERE id=?", [commitment_id])
        return rows[0]

    def _event(self, commitment_id: str, actor_sub: str | None, from_status: str | None, to_status: str, from_week: str | None, to_week: str) -> None:
        if from_status == to_status and from_week == to_week:
            return
        self.db.query(
            "INSERT INTO commitment_events(id,commitment_id,actor_sub,from_status,to_status,from_week,to_week,created_at) VALUES(?,?,?,?,?,?,?,?)",
            [uuid4().hex, commitment_id, actor_sub, from_status, to_status, from_week, to_week, now_iso()],
        )

    def _existing_request(self, owner_sub: str, request_id: str, fingerprint: str, table: str) -> dict[str, Any] | None:
        if not REQUEST_RE.fullmatch(request_id):
            raise ValueError("client_request_id must be 8 to 80 letters, numbers, underscores or hyphens")
        column = "owner_sub" if table == "commitments" else "author_sub"
        rows = self.db.query(f"SELECT * FROM {table} WHERE {column}=? AND client_request_id=?", [owner_sub, request_id])
        if not rows:
            return None
        existing_fields = rows[0]
        if table == "commitments":
            existing_fingerprint = commitment_fingerprint(existing_fields)
        else:
            existing_fingerprint = note_fingerprint(existing_fields)
        if existing_fingerprint != fingerprint:
            raise RequestConflict(request_id)
        return rows[0]

    @staticmethod
    def _note(row: dict[str, Any]) -> dict[str, Any]:
        return {**row, "pinned": bool(row.get("pinned"))}

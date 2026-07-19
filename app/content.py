from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from typing import Any

import yaml

ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
LINK_RE = re.compile(r"^- \[([^]]+)]\(([^)]+)\)$", re.MULTILINE)
CANONICAL_FIELDS = {"tenant_id", "client_id", "project_id", "title", "date_time", "participants"}
OKF_REQUIRED_FIELDS = {"type", "title", "description", "sources", "timestamp"}
OKF_TYPES = {"context", "meeting", "decision", "risk"}
WIKI_SCOPE_FIELDS = {"tenant_id", "client_id", "project_id"}


class _NoTimestampSafeLoader(yaml.SafeLoader):
    """Keep YAML timestamps as strings so the OKF timestamp syntax is enforceable."""


_NoTimestampSafeLoader.yaml_implicit_resolvers = {
    first: [resolver for resolver in resolvers if resolver[0] != "tag:yaml.org,2002:timestamp"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def validate_identifier(value: str, field_name: str) -> str:
    if not ID_RE.fullmatch(value):
        raise ValueError(f"{field_name} must match [a-zA-Z0-9_-]+")
    return value


def slugify(title: str) -> str:
    normalized = unicodedata.normalize("NFKD", title).lower()
    normalized = "".join(c for c in normalized if not unicodedata.combining(c))
    slug = re.sub(r"[^\w]+", "-", normalized, flags=re.UNICODE).strip("-")
    if not slug:
        raise ValueError("title must contain at least one letter or number")
    return slug


def _parse_front_matter(markdown: str, *, required: bool = False) -> tuple[dict[str, Any], str]:
    if not markdown.startswith("---") or not re.match(r"^---\s*\r?\n", markdown):
        if required:
            raise ValueError("front matter YAML is required")
        return {}, markdown
    match = re.compile(r"^---\s*$|^\.\.\.\s*$", re.MULTILINE).search(markdown, 4)
    if match is None:
        raise ValueError("front matter YAML is not closed")
    raw_yaml = markdown[4:match.start()]
    try:
        parsed = yaml.load(raw_yaml, Loader=_NoTimestampSafeLoader) or {}
    except yaml.YAMLError as exc:
        raise ValueError("front matter YAML is invalid") from exc
    if not isinstance(parsed, dict):
        raise ValueError("front matter YAML must be a mapping")
    body = markdown[match.end():].lstrip("\r\n")
    return parsed, body


def split_front_matter(markdown: str) -> tuple[dict[str, Any], str]:
    metadata, body = _parse_front_matter(markdown)
    return {key: value for key, value in metadata.items() if key not in CANONICAL_FIELDS}, body


def validate_okf_document(markdown: str, *, tenant_id: str, client_id: str, project_id: str, required_type: str | None = None) -> dict[str, Any]:
    """Validate the front matter contract for a derived document in ``wiki/``."""
    metadata, _ = _parse_front_matter(markdown, required=True)
    missing = sorted(field for field in OKF_REQUIRED_FIELDS if field not in metadata)
    if missing:
        raise ValueError(f"OKF front matter is missing required fields: {', '.join(missing)}")
    duplicated_scope = sorted(field for field in WIKI_SCOPE_FIELDS if field in metadata)
    if duplicated_scope:
        raise ValueError(f"OKF front matter must not duplicate path scope fields: {', '.join(duplicated_scope)}")

    document_type = metadata["type"]
    if not isinstance(document_type, str) or document_type not in OKF_TYPES:
        raise ValueError("OKF type must be one of: context, meeting, decision, risk")
    if required_type and document_type != required_type:
        raise ValueError(f"OKF type must be {required_type}")
    for field in ("title", "description"):
        if not isinstance(metadata[field], str) or not metadata[field].strip():
            raise ValueError(f"OKF {field} must be a non-empty string")

    timestamp = metadata["timestamp"]
    if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
        raise ValueError("OKF timestamp must be an RFC 3339 UTC timestamp ending in Z")
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ValueError("OKF timestamp must be an RFC 3339 UTC timestamp ending in Z") from exc
    if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() != UTC.utcoffset(parsed_timestamp):
        raise ValueError("OKF timestamp must be an RFC 3339 UTC timestamp ending in Z")

    sources = metadata["sources"]
    if not isinstance(sources, list) or not sources or any(not isinstance(source, str) or not source for source in sources):
        raise ValueError("OKF sources must be a non-empty list of source keys")
    if len(sources) != len(set(sources)):
        raise ValueError("OKF sources must not contain duplicate source keys")
    expected_prefix = f"sources/{tenant_id}/{client_id}/{project_id}/"
    if any(not source.startswith(expected_prefix) or not source.endswith(".md") for source in sources):
        raise ValueError("OKF sources must belong to the same tenant, client, and project")
    return metadata


def build_okf_document(*, document_type: str, title: str, description: str, sources: list[str], timestamp: datetime, body: str = "") -> str:
    """Render a validated OKF document generated by the service."""
    utc_timestamp = timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp.astimezone(UTC)
    metadata = {
        "type": document_type,
        "title": title,
        "description": description,
        "sources": sources,
        "timestamp": utc_timestamp.isoformat().replace("+00:00", "Z"),
    }
    return "---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip() + "\n---\n\n" + body


def build_source_markdown(markdown: str, *, tenant_id: str, client_id: str, project_id: str, title: str, date_time: datetime, participants: list[str]) -> str:
    extras, body = split_front_matter(markdown)
    utc_datetime = date_time.replace(tzinfo=UTC) if date_time.tzinfo is None else date_time.astimezone(UTC)
    metadata: dict[str, Any] = {**extras, "tenant_id": tenant_id, "client_id": client_id, "project_id": project_id, "title": title, "date_time": utc_datetime.isoformat().replace("+00:00", "Z"), "participants": participants}
    return "---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip() + "\n---\n\n" + body


def markdown_links(markdown: str) -> list[dict[str, str]]:
    return [{"name": name, "href": href} for name, href in LINK_RE.findall(markdown)]


# The indexes and log are internal, agent-oriented navigation artifacts.
def tenant_index(tenant_id: str) -> str:
    return f"# Wiki: tenant {tenant_id}\n\n## Clients\n\n"


def client_index(client_id: str) -> str:
    return f"# Wiki: client {client_id}\n\n## Projects\n\n"


def project_index(project_id: str) -> str:
    return f"# Wiki: project {project_id}\n\n## Sources\n\n"


def log_header(tenant_id: str) -> str:
    return f"# Activity log: tenant {tenant_id}\n\n"


def append_link(markdown: str, name: str, href: str) -> str:
    line = f"- [{name}]({href})"
    return markdown if line in markdown else markdown.rstrip() + "\n" + line + "\n"

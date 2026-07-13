from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from typing import Any

import yaml

ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
LINK_RE = re.compile(r"^- \[([^]]+)]\(([^)]+)\)$", re.MULTILINE)
CANONICAL_FIELDS = {"tenant_id", "client_id", "project_id", "title", "date_time", "participants"}


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


def split_front_matter(markdown: str) -> tuple[dict[str, Any], str]:
    if not markdown.startswith("---") or not re.match(r"^---\s*\r?\n", markdown):
        return {}, markdown
    match = re.compile(r"^---\s*$|^\.\.\.\s*$", re.MULTILINE).search(markdown, 4)
    if match is None:
        raise ValueError("front matter YAML is not closed")
    raw_yaml = markdown[4:match.start()]
    try:
        parsed = yaml.safe_load(raw_yaml) or {}
    except yaml.YAMLError as exc:
        raise ValueError("front matter YAML is invalid") from exc
    if not isinstance(parsed, dict):
        raise ValueError("front matter YAML must be a mapping")
    body = markdown[match.end():].lstrip("\r\n")
    return {key: value for key, value in parsed.items() if key not in CANONICAL_FIELDS}, body


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

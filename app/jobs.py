from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Protocol

from app.identity import D1Store


JOB_STATUSES = {"pending", "running", "succeeded", "failed"}


def job_id_for_source(source_key: str) -> str:
    return sha256(source_key.encode("utf-8")).hexdigest()[:32]


class JobStore(Protocol):
    def create_pending(self, *, job_id: str, source_key: str, tenant_id: str, client_id: str, project_id: str) -> None: ...
    def get(self, tenant_id: str, job_id: str) -> dict[str, Any] | None: ...
    def list(self, tenant_id: str, *, client_id: str | None, project_id: str | None, limit: int) -> list[dict[str, Any]]: ...


class D1JobStore:
    def __init__(self, d1: D1Store) -> None:
        self.d1 = d1

    def create_pending(self, *, job_id: str, source_key: str, tenant_id: str, client_id: str, project_id: str) -> None:
        self.d1.query(
            "INSERT INTO librarian_jobs(job_id,source_key,tenant_id,client_id,project_id,status,attempts,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'pending',0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP) "
            "ON CONFLICT(source_key) DO NOTHING",
            [job_id, source_key, tenant_id, client_id, project_id],
        )

    def get(self, tenant_id: str, job_id: str) -> dict[str, Any] | None:
        rows = self.d1.query(
            "SELECT job_id,source_key,tenant_id,client_id,project_id,status,attempts,model,thinking_level,"
            "created_at,started_at,completed_at,updated_at,output_keys,error_code,input_tokens,output_tokens,latency_ms "
            "FROM librarian_jobs WHERE tenant_id=? AND job_id=?",
            [tenant_id, job_id],
        )
        return self._public(rows[0]) if rows else None

    def list(self, tenant_id: str, *, client_id: str | None, project_id: str | None, limit: int) -> list[dict[str, Any]]:
        clauses, params = ["tenant_id=?"], [tenant_id]
        if client_id:
            clauses.append("client_id=?"); params.append(client_id)
        if project_id:
            clauses.append("project_id=?"); params.append(project_id)
        params.append(limit)
        rows = self.d1.query(
            "SELECT job_id,source_key,tenant_id,client_id,project_id,status,attempts,model,thinking_level,"
            "created_at,started_at,completed_at,updated_at,output_keys,error_code,input_tokens,output_tokens,latency_ms "
            f"FROM librarian_jobs WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ?",
            params,
        )
        return [self._public(row) for row in rows]

    @staticmethod
    def _public(row: dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        raw_outputs = item.get("output_keys")
        if isinstance(raw_outputs, str):
            try:
                item["output_keys"] = json.loads(raw_outputs)
            except json.JSONDecodeError:
                item["output_keys"] = []
        elif raw_outputs is None:
            item["output_keys"] = []
        return item

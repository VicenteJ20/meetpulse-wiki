from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from datetime import datetime
from typing import Protocol

import boto3
from botocore.exceptions import ClientError, EndpointConnectionError

from app.config import Settings


class StorageError(Exception):
    """The object store could not complete an operation."""


class ObjectNotFound(StorageError):
    pass


class PreconditionFailed(StorageError):
    pass


@dataclass(frozen=True)
class StoredObject:
    text: str
    etag: str
    last_modified: datetime | None = None


@dataclass(frozen=True)
class StoredBytes:
    data: bytes
    etag: str
    content_type: str | None = None
    sha256: str | None = None
    last_modified: datetime | None = None


class ObjectStorage(Protocol):
    def get_text(self, key: str) -> StoredObject: ...

    def get_bytes(self, key: str) -> StoredBytes: ...

    def put_if_absent(self, key: str, text: str) -> None: ...

    def put_if_match(self, key: str, text: str, etag: str) -> None: ...

    def put_bytes_if_absent(self, key: str, data: bytes, *, content_type: str, sha256_hex: str) -> None: ...

    def list_keys(self, prefix: str) -> list[str]: ...


class R2Storage:
    """Minimal S3-compatible adapter for Cloudflare R2."""

    def __init__(self, settings: Settings) -> None:
        self.bucket = settings.r2_bucket_name
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.r2_endpoint_url,
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            region_name=settings.r2_region,
        )

    @staticmethod
    def _last_modified(response) -> datetime | None:
        metadata = response.get("Metadata", {})
        preserved = metadata.get("original-last-modified") if metadata.get("consolidation-import") == "1" else None
        if preserved:
            try:
                parsed = datetime.fromisoformat(preserved.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    return parsed
            except ValueError:
                pass
        return response.get("LastModified")

    @classmethod
    def from_environment(cls) -> "R2Storage":
        return cls(Settings())

    def get_text(self, key: str) -> StoredObject:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            return StoredObject(
                text=response["Body"].read().decode("utf-8"),
                etag=response["ETag"].strip('"'),
                last_modified=self._last_modified(response),
            )
        except ClientError as exc:
            if exc.response["Error"].get("Code") in {"NoSuchKey", "404", "NotFound"}:
                raise ObjectNotFound(key) from exc
            raise StorageError(str(exc)) from exc
        except (EndpointConnectionError, UnicodeDecodeError) as exc:
            raise StorageError(str(exc)) from exc

    def get_bytes(self, key: str) -> StoredBytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            data = response["Body"].read()
            metadata = response.get("Metadata", {})
            return StoredBytes(
                data=data,
                etag=response["ETag"].strip('"'),
                content_type=response.get("ContentType"),
                sha256=metadata.get("sha256") or sha256(data).hexdigest(),
                last_modified=self._last_modified(response),
            )
        except ClientError as exc:
            if exc.response["Error"].get("Code") in {"NoSuchKey", "404", "NotFound"}:
                raise ObjectNotFound(key) from exc
            raise StorageError(str(exc)) from exc
        except EndpointConnectionError as exc:
            raise StorageError(str(exc)) from exc

    def put_if_absent(self, key: str, text: str) -> None:
        self._put(key, text, IfNoneMatch="*")

    def put_if_match(self, key: str, text: str, etag: str) -> None:
        self._put(key, text, IfMatch=etag)

    def put_bytes_if_absent(self, key: str, data: bytes, *, content_type: str, sha256_hex: str) -> None:
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                Metadata={"sha256": sha256_hex},
                IfNoneMatch="*",
            )
        except ClientError as exc:
            if exc.response["Error"].get("Code") in {"PreconditionFailed", "412"}:
                raise PreconditionFailed(key) from exc
            raise StorageError(str(exc)) from exc
        except EndpointConnectionError as exc:
            raise StorageError(str(exc)) from exc

    def _put(self, key: str, text: str, **conditions: str) -> None:
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=text.encode("utf-8"),
                ContentType="text/markdown; charset=utf-8",
                **conditions,
            )
        except ClientError as exc:
            if exc.response["Error"].get("Code") in {"PreconditionFailed", "412"}:
                raise PreconditionFailed(key) from exc
            raise StorageError(str(exc)) from exc
        except EndpointConnectionError as exc:
            raise StorageError(str(exc)) from exc

    def list_keys(self, prefix: str) -> list[str]:
        try:
            paginator = self.client.get_paginator("list_objects_v2")
            return [
                item["Key"]
                for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix)
                for item in page.get("Contents", [])
            ]
        except (ClientError, EndpointConnectionError) as exc:
            raise StorageError(str(exc)) from exc

"""Reset only user-facing MeetPulse Wiki data in the configured R2 bucket.

Agent configuration and unrelated bucket objects are deliberately preserved.
"""

from __future__ import annotations

from pathlib import Path

import boto3


def env_file_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def main() -> None:
    values = env_file_values(Path(".env"))
    client = boto3.client("s3", endpoint_url=values["R2_ENDPOINT_URL"], aws_access_key_id=values["R2_ACCESS_KEY_ID"], aws_secret_access_key=values["R2_SECRET_ACCESS_KEY"], region_name=values.get("R2_REGION", "auto"))
    bucket = values["R2_BUCKET_NAME"]
    keys = []
    for prefix in ("sources/", "wiki/"):
        keys.extend(item["Key"] for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix) for item in page.get("Contents", []))
    for start in range(0, len(keys), 1000):
        client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": key} for key in keys[start:start + 1000]], "Quiet": True})
    print(f"Deleted {len(keys)} user Wiki objects under sources/ and wiki/.")


if __name__ == "__main__":
    main()

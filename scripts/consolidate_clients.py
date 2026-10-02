"""Preserve-first Wiki consolidation. Backups and reports never contain credentials.

Run from the Wiki repository with its virtualenv. Phases are explicit and restartable.
Original objects remain available for old links; canonical indexes are published last.
"""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path.cwd()))
from app.config import Settings
from app.content import parse_front_matter, slugify
from app.identity import D1Store
from app.jobs import job_id_for_source
from app.service import WikiService
from app.storage import R2Storage
import yaml

TENANTS = ("vicente-inovabiz", "vicente-s-tenant")


def digest(data):
    return sha256(data).hexdigest()


def canonical_key(key):
    parts = key.split("/")
    if parts[0] in {"wiki", "sources", "raw"} and len(parts) >= 4:
        parts[2] = slugify(parts[2])
        if len(parts) >= 5:
            parts[3] = slugify(parts[3])
    return "/".join(parts)


def rewrite_text(text, mapping, key):
    # Replace complete object paths, never arbitrary client names in meeting prose.
    pattern = re.compile(r"(?<![A-Za-z0-9_-])(?:" + "|".join(re.escape(k) for k in sorted(mapping, key=len, reverse=True)) + r")(?![A-Za-z0-9_.-])") if mapping else None
    if pattern:
        text = pattern.sub(lambda m: mapping[m.group()], text)
    if key.startswith("sources/") and text.startswith("---"):
        metadata, _ = parse_front_matter(text)
        parts = canonical_key(key).split("/")
        # Preserve original markdown bytes and formatting except scope and references.
        for name, value in (("client_id", parts[2]), ("project_id", parts[3])):
            text = re.sub(r"(?m)^" + name + r":[^\n]*$", name + ": " + value, text, count=1)
    return text


def equivalent(a, b):
    if a == b:
        return True
    try:
        am, ab = parse_front_matter(a.decode())
        bm, bb = parse_front_matter(b.decode())
        return am == bm and ab == bb
    except (UnicodeError, ValueError):
        return False


def conflict_key(key, old):
    p = Path(key)
    return str(p.with_name(p.stem + "-legacy-" + digest(old.encode())[:10] + p.suffix)).replace("\\", "/")


def prepare(objects):
    # Canonical objects win every collision. Missing targets prefer canonical project IDs.
    legacy = sorted((k for k in objects if canonical_key(k) != k), key=lambda k: (k.split('/')[3] != slugify(k.split('/')[3]) if len(k.split('/')) > 4 else False, k))
    mapping = {}
    writes = {}
    conflicts = []
    # Resolve raw paths before source references, then source paths before derived references.
    for kind in ("raw", "sources", "wiki"):
        for old in [k for k in legacy if k.startswith(kind + "/")]:
            if old.endswith("/index.md"):
                continue
            new = canonical_key(old)
            obj = objects[old]
            data = base64.b64decode(obj["data"])
            if kind != "raw":
                data = rewrite_text(data.decode(), mapping, old).encode()
            current = writes.get(new)
            existing = current["data"] if current else (base64.b64decode(objects[new]["data"]) if new in objects else None)
            if existing is not None and equivalent(data, existing):
                mapping[old] = new
                continue
            if existing is not None:
                if new.endswith("/context.md"):
                    # Old context is historical evidence, never a replacement for live context.
                    new = new.removesuffix("context.md") + "history/context-legacy-" + digest(old.encode())[:10] + ".md"
                else:
                    new = conflict_key(new, old)
                conflicts.append({"original": old, "preserved_as": new})
                if new in objects and not equivalent(data, base64.b64decode(objects[new]["data"])):
                    raise RuntimeError("Historical conflict destination is occupied: " + new)
            mapping[old] = new
            writes[new] = {"original": old, "data": data}
    # A second pass rewrites all cross-document references, including later legacy objects.
    for new, item in writes.items():
        if not new.startswith("raw/"):
            item["data"] = rewrite_text(item["data"].decode(), mapping, item["original"]).encode()
    return mapping, writes, conflicts


def services():
    cfg = Settings()
    return R2Storage(cfg), D1Store(cfg.cloudflare_account_id, cfg.cloudflare_d1_database_id, cfg.cloudflare_d1_api_token)


def snapshot(folder):
    storage, db = services()
    folder.mkdir(parents=True, exist_ok=False)
    keys = sorted(k for tenant in TENANTS for kind in ("wiki", "sources", "raw") for k in storage.list_keys(f"{kind}/{tenant}/"))
    def fetch(key):
        r = storage.client.get_object(Bucket=storage.bucket, Key=key)
        data = r["Body"].read()
        return key, {"data": base64.b64encode(data).decode(), "sha256": digest(data), "etag": r["ETag"], "last_modified": r["LastModified"].isoformat(), "content_type": r.get("ContentType", "application/octet-stream"), "metadata": r.get("Metadata", {})}
    objects = dict(ThreadPoolExecutor(max_workers=8).map(fetch, keys))
    jobs = db.query("SELECT * FROM librarian_jobs", [])
    locks = db.query("SELECT * FROM librarian_project_locks", [])
    (folder / "snapshot.json").write_text(json.dumps({"objects": objects, "jobs": jobs, "locks": locks}))
    mapping, writes, conflicts = prepare(objects)
    report = {"backed_up_objects": len(objects), "legacy_objects": len(mapping), "new_objects": len(writes), "conflicts": conflicts, "mapping": mapping}
    (folder / "plan.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({k:v for k,v in report.items() if k != 'mapping'}), flush=True)


def apply(folder):
    storage, db = services()
    snapshot = json.loads((folder / "snapshot.json").read_text())
    objects = snapshot["objects"]
    mapping, writes, conflicts = prepare(objects)
    # Refuse drift before mutation. The queue should be paused and locks drained.
    assert not db.query("SELECT project_key FROM librarian_project_locks WHERE lease_until > CURRENT_TIMESTAMP", [])
    for tenant in TENANTS:
        live = {k for kind in ("wiki", "sources", "raw") for k in storage.list_keys(f"{kind}/{tenant}/")}
        assert live == {k for k in objects if k.split('/')[1] == tenant}, "Object inventory changed"
    def check(key):
        head = storage.client.head_object(Bucket=storage.bucket, Key=key)
        if head["ETag"] != objects[key]["etag"]:
            raise RuntimeError("Object changed since backup: " + key)
    list(ThreadPoolExecutor(max_workers=8).map(check, objects))
    journal = []
    for i, (new, item) in enumerate(writes.items(), 1):
        original = objects[item["original"]]
        md = {**original["metadata"], "consolidation-import": "1", "original-last-modified": original["last_modified"], "consolidation-origin": item["original"]}
        if new.startswith('raw/'):
            md['sha256'] = digest(item['data'])
        storage.client.put_object(Bucket=storage.bucket, Key=new, Body=item["data"], ContentType=original["content_type"], Metadata=md, IfNoneMatch="*")
        saved = storage.client.get_object(Bucket=storage.bucket, Key=new)["Body"].read()
        assert saved == item['data'], "Saved bytes differ"
        journal.append({"key":new,"original":item['original'],"sha256":digest(saved)})
        (folder / "written.json").write_text(json.dumps(journal, indent=2))
        if i % 20 == 0:
            print(f"Verified {i}/{len(writes)} copied objects", flush=True)
    # Add canonical job records without changing existing records or falsifying original statuses.
    for job in snapshot['jobs']:
        old = job['source_key']
        if old not in mapping or mapping[old] == old:
            continue
        new = mapping[old]
        parts = new.split('/')
        row = dict(job)
        row.update(job_id=job_id_for_source(new),source_key=new,client_id=parts[2],project_id=parts[3])
        if row.get('output_keys'):
            row['output_keys'] = json.dumps([mapping.get(k,k) for k in json.loads(row['output_keys'])])
        # Cached model payload describes the old scope; keep only in the backup/original record.
        row['analysis_payload'] = None
        cols = list(row)
        db.query(f"INSERT INTO librarian_jobs({','.join(cols)}) VALUES({','.join('?' for _ in cols)}) ON CONFLICT(source_key) DO NOTHING",[row[k] for k in cols])
    (folder / 'applied.json').write_text(json.dumps({'objects':len(journal),'jobs_preserved':True}))
    verify(folder, publish=True)


def verify(folder, publish=False):
    storage, db = services()
    snapshot = json.loads((folder / 'snapshot.json').read_text())
    objects = snapshot['objects']
    mapping, writes, conflicts = prepare(objects)
    # Every original (including all canonical content) remains exactly as backed up.
    def unchanged(key):
        current=storage.client.get_object(Bucket=storage.bucket,Key=key)['Body'].read()
        assert digest(current)==objects[key]['sha256'], 'Original content changed: '+key
    immutable = [k for k in objects if not k.endswith('/index.md')]
    list(ThreadPoolExecutor(max_workers=8).map(unchanged, immutable))
    for key,item in writes.items():
        assert storage.client.get_object(Bucket=storage.bucket,Key=key)['Body'].read()==item['data']
    live = {k for tenant in TENANTS for kind in ('wiki','sources','raw') for k in storage.list_keys(f'{kind}/{tenant}/')}
    # Validate front matter references for all imported meeting and knowledge documents.
    references=0
    for key,item in writes.items():
        if key.startswith('raw/'):
            continue
        metadata,_=parse_front_matter(item['data'].decode())
        for field in ('sources','raw_sources','related','supersedes'):
            for ref in metadata.get(field,[]) or []:
                assert ref in live, f'Missing {field} reference: {key} -> {ref}'
                references+=1
    if publish:
        wiki = WikiService(storage)
        projects={tuple(canonical_key(k).split('/')[1:4]) for k in objects if k.startswith('wiki/') and k.endswith('/index.md') and len(k.split('/'))==5}
        for tenant,client,project in sorted(projects):
            key=f'wiki/{tenant}/{client}/{project}/index.md'
            wiki._replace_if_changed(key,wiki._render_project_index(tenant,client,project))
        for tenant,client in sorted({(t,c) for t,c,p in projects}):
            key=f'wiki/{tenant}/{client}/index.md'
            wiki._replace_if_changed(key,wiki._render_client_index(tenant,client))
        for tenant in TENANTS:
            wiki._replace_if_changed(f'wiki/{tenant}/index.md',wiki._render_tenant_index(tenant))
    report={'original_content_verified':len(immutable),'new_objects_verified':len(writes),'references_verified':references,'historical_conflicts_preserved':len(conflicts)}
    (folder/'verification.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report),flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('phase',choices=['snapshot','apply','verify'])
    parser.add_argument('--folder',required=True,type=Path)
    args=parser.parse_args()
    {'snapshot':snapshot,'apply':apply,'verify':verify}[args.phase](args.folder)

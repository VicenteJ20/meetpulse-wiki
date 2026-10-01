from datetime import UTC, datetime
import base64

from app.content import normalize_scope_identifier
from app.storage import R2Storage
from scripts.consolidate_clients import prepare


def obj(text):
    return {"data": base64.b64encode(text.encode()).decode()}


def test_normalized_context_wins_and_legacy_sources_keep_correct_provenance():
    canonical_context = "---\ntype: context\nsources:\n- sources/t/kaufmann/migracion-apim/meeting.md\n---\nCurrent context"
    source = "---\nclient_id: Kaufmann\nproject_id: Migracion-APIM\nraw_sources:\n- raw/t/Kaufmann/Migracion-APIM/meeting/transcript.txt\n---\nOld meeting"
    objects = {
        "wiki/t/kaufmann/migracion-apim/context.md": obj(canonical_context),
        "wiki/t/Kaufmann/Migracion-APIM/context.md": obj("---\ntype: context\nsources:\n- sources/t/Kaufmann/Migracion-APIM/meeting.md\n---\nOld context"),
        "sources/t/Kaufmann/Migracion-APIM/meeting.md": obj(source),
        "sources/t/kaufmann/migracion-apim/meeting.md": obj("---\nclient_id: kaufmann\nproject_id: migracion-apim\n---\nCurrent meeting"),
        "raw/t/Kaufmann/Migracion-APIM/meeting/transcript.txt": obj("original transcript"),
    }
    mapping, writes, conflicts = prepare(objects)
    assert "wiki/t/kaufmann/migracion-apim/context.md" not in writes
    assert "sources/t/kaufmann/migracion-apim/meeting.md" not in writes
    migrated_source = mapping["sources/t/Kaufmann/Migracion-APIM/meeting.md"]
    assert "-legacy-" in migrated_source
    imported = writes[migrated_source]["data"].decode()
    assert "client_id: kaufmann" in imported
    assert "project_id: migracion-apim" in imported
    assert "raw/t/kaufmann/migracion-apim/meeting/transcript.txt" in imported
    historical_context = writes[mapping["wiki/t/Kaufmann/Migracion-APIM/context.md"]]["data"].decode()
    assert migrated_source in historical_context
    assert len(conflicts) == 2
    assert objects["wiki/t/kaufmann/migracion-apim/context.md"] == obj(canonical_context)


def test_equivalent_legacy_documents_are_deduplicated():
    objects = {
        "sources/t/Client/Project/a.md": obj("---\nclient_id: Client\nproject_id: Project\n---\nSame"),
        "sources/t/client/project/a.md": obj("---\nclient_id: client\nproject_id: project\n---\nSame"),
    }
    mapping, writes, conflicts = prepare(objects)
    assert mapping["sources/t/Client/Project/a.md"] == "sources/t/client/project/a.md"
    assert not writes and not conflicts


def test_client_and_project_labels_normalize_but_path_traversal_is_rejected():
    import pytest
    assert normalize_scope_identifier(" Consultoría Arquitectura ", "project_id") == "consultoria-arquitectura"
    assert normalize_scope_identifier("Zurich Santander", "client_id") == "zurich-santander"
    for value in ("../client", "a/b", "a\\b", "..."):
        with pytest.raises(ValueError):
            normalize_scope_identifier(value, "client_id")


def test_imported_documents_keep_original_last_modified():
    date = datetime(2026, 9, 28, tzinfo=UTC)
    response = {"LastModified": datetime(2026, 10, 1, tzinfo=UTC), "Metadata": {"consolidation-import": "1", "original-last-modified": date.isoformat()}}
    assert R2Storage._last_modified(response) == date

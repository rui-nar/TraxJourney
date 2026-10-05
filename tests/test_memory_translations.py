"""Tests for memory translation endpoints and caching behaviour."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import models.db as db_module
from api.deps import get_current_user
from api.memories import _is_encrypted_envelope, _utc_now
from api.memories import router as memories_router
from models.project_db import DBMemory, DBMemoryTranslation, DBProject
from models.user import UserInfo


# ---------------------------------------------------------------------------
# Helpers — unit-test _utc_now and the translate_text helper in isolation
# ---------------------------------------------------------------------------

class TestUtcNowFormat:
    def test_ends_with_z(self):
        ts = _utc_now()
        assert ts.endswith("Z")

    def test_length(self):
        assert len(_utc_now()) == 20  # "YYYY-MM-DDTHH:MM:SSZ"


# ---------------------------------------------------------------------------
# translate_text helper — mock httpx to avoid real API calls
# ---------------------------------------------------------------------------

class TestTranslateText:
    @pytest.mark.anyio
    async def test_returns_translated_text(self):
        from api.translations import translate_text

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "data": {"translations": [{"translatedText": "Bonjour"}]}
        }

        with patch("api.translations.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await translate_text("Hello", "fr")

        assert result == "Bonjour"

    @pytest.mark.anyio
    async def test_passes_source_lang_when_provided(self):
        from api.translations import translate_text

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "data": {"translations": [{"translatedText": "Hola"}]}
        }

        with patch("api.translations.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            await translate_text("Hello", "es", source_lang="en")

        call_kwargs = mock_client.post.call_args[1]
        assert call_kwargs["params"]["source"] == "en"

    @pytest.mark.anyio
    async def test_http_error_propagates(self):
        """A non-2xx Google response must raise, so get_translation's except
        fires, logs, and surfaces a 502 (the prod symptom in #24 / issue follow-up)."""
        import httpx
        from api.translations import translate_text

        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "403 Forbidden", request=MagicMock(), response=MagicMock()
            )
        )

        with patch("api.translations.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            with pytest.raises(httpx.HTTPStatusError):
                await translate_text("Hello", "pt")

    @pytest.mark.anyio
    async def test_no_source_lang_omits_source_param(self):
        from api.translations import translate_text

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "data": {"translations": [{"translatedText": "Ciao"}]}
        }

        with patch("api.translations.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            await translate_text("Hello", "it")

        call_kwargs = mock_client.post.call_args[1]
        assert "source" not in call_kwargs["params"]


# ---------------------------------------------------------------------------
# DBMemoryTranslation model — basic field access
# ---------------------------------------------------------------------------

class TestDBMemoryTranslation:
    def test_fields_set_correctly(self):
        from models.project_db import DBMemoryTranslation

        row = DBMemoryTranslation(
            memory_id=1,
            lang_code="fr",
            name="Bonjour",
            description="Un beau souvenir",
            created_at="2026-05-30T10:00:00Z",
        )
        assert row.memory_id == 1
        assert row.lang_code == "fr"
        assert row.name == "Bonjour"
        assert row.description == "Un beau souvenir"

    def test_name_and_description_nullable(self):
        from models.project_db import DBMemoryTranslation

        row = DBMemoryTranslation(memory_id=5, lang_code="de")
        assert row.name is None
        assert row.description is None


# ---------------------------------------------------------------------------
# Project languages field propagation
# ---------------------------------------------------------------------------

class TestProjectLanguages:
    def test_project_default_languages_empty(self):
        from src.models.project import Project

        p = Project(name="test")
        assert p.languages == []

    def test_project_languages_set(self):
        from src.models.project import Project

        p = Project(name="test", languages=["fr", "de"])
        assert p.languages == ["fr", "de"]

    def test_languages_serialised_in_to_dict(self):
        from src.project.project_io import ProjectIO
        from src.models.project import Project

        p = Project(name="trip", languages=["pt", "es"])
        d = ProjectIO.to_dict(p)
        assert d["languages"] == ["pt", "es"]

    def test_empty_languages_serialised_as_empty_list(self):
        from src.project.project_io import ProjectIO
        from src.models.project import Project

        p = Project(name="trip")
        d = ProjectIO.to_dict(p)
        assert d["languages"] == []


# ---------------------------------------------------------------------------
# _is_encrypted_envelope — mirrors the client's EncryptedField.isEnvelope
# format check (e2ee_crypto.dart), issue #27
# ---------------------------------------------------------------------------

class TestIsEncryptedEnvelope:
    def test_none_and_empty_are_not_envelopes(self):
        assert _is_encrypted_envelope(None) is False
        assert _is_encrypted_envelope("") is False

    def test_plaintext_is_not_an_envelope(self):
        assert _is_encrypted_envelope("A lovely afternoon in Lisbon") is False
        assert _is_encrypted_envelope("v1 without dots") is False

    def test_wrong_version_prefix_is_not_an_envelope(self):
        assert _is_encrypted_envelope("v2.YWJj.ZGVm") is False

    def test_wrong_part_count_is_not_an_envelope(self):
        assert _is_encrypted_envelope("v1.YWJj") is False
        assert _is_encrypted_envelope("v1.YWJj.ZGVm.extra") is False

    def test_valid_envelope_shape_is_detected(self):
        assert _is_encrypted_envelope("v1.YWJj.ZGVm") is True


# ---------------------------------------------------------------------------
# GET .../translations/{lang} and PUT .../{id} — encrypted-memory handling
# (issue #27: never send ciphertext to the translator; purge stale cached
# translations once a memory's content becomes ciphertext)
# ---------------------------------------------------------------------------

@pytest.fixture
def env(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    SQLModel.metadata.create_all(engine)

    with Session(engine) as sess:
        user = UserInfo(display_name="Alice", email="alice@example.com")
        sess.add(user)
        sess.commit()
        sess.refresh(user)
        user_id = user.id

        project = DBProject(user_info_id=user_id, name="My Trip",
                             languages_json=json.dumps(["fr"]))
        sess.add(project)
        sess.commit()
        sess.refresh(project)
        project_id = project.id

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(user_id), "email": "alice@example.com"}
    app.include_router(memories_router)
    client = TestClient(app)
    return client, engine, project_id


def _insert_memory(engine, project_id, name=None, description=None) -> int:
    with Session(engine) as sess:
        row = DBMemory(project_id=project_id, date="2025-06-01", geo_mode="custom",
                        name=name, description=description)
        sess.add(row)
        sess.commit()
        sess.refresh(row)
        return row.id


def _insert_cached_translation(engine, memory_id, lang_code="fr") -> None:
    with Session(engine) as sess:
        sess.add(DBMemoryTranslation(
            memory_id=memory_id, lang_code=lang_code,
            name="Bonjour", description="Un souvenir", created_at=_utc_now(),
        ))
        sess.commit()


class TestGetTranslationRejectsEncrypted:
    def test_rejects_when_name_is_an_envelope(self, env):
        client, engine, project_id = env
        mem_id = _insert_memory(engine, project_id, name="v1.YWJj.ZGVm", description="plain")
        with patch("api.memories.translate_text", new_callable=AsyncMock) as mock_translate:
            r = client.get(f"/api/memories/{mem_id}/translations/fr")
        assert r.status_code == 409
        mock_translate.assert_not_called()

    def test_rejects_when_description_is_an_envelope(self, env):
        client, engine, project_id = env
        mem_id = _insert_memory(engine, project_id, name="A place", description="v1.YWJj.ZGVm")
        with patch("api.memories.translate_text", new_callable=AsyncMock) as mock_translate:
            r = client.get(f"/api/memories/{mem_id}/translations/fr")
        assert r.status_code == 409
        mock_translate.assert_not_called()

    def test_plaintext_memory_still_translates(self, env):
        client, engine, project_id = env
        mem_id = _insert_memory(engine, project_id, name="A place", description="Some notes")
        mock_translate = AsyncMock(side_effect=["Un lieu", "Quelques notes"])
        with patch("api.memories.translate_text", mock_translate):
            r = client.get(f"/api/memories/{mem_id}/translations/fr")
        assert r.status_code == 200
        assert r.json() == {"lang_code": "fr", "name": "Un lieu", "description": "Quelques notes"}


class TestUpdateMemoryPurgesTranslationCache:
    def test_purges_cache_when_content_becomes_encrypted(self, env):
        client, engine, project_id = env
        mem_id = _insert_memory(engine, project_id, name="A place", description="Some notes")
        _insert_cached_translation(engine, mem_id)
        # Envelopes are only accepted on an encrypted owner's trip (#505).
        with Session(engine) as sess:
            owner = sess.get(UserInfo, sess.get(DBProject, project_id).user_info_id)
            owner.encryption_enabled = True
            sess.add(owner)
            sess.commit()

        r = client.put(f"/api/memories/{mem_id}", json={
            "date": "2025-06-01", "geo_mode": "custom",
            "name": "v1.YWJj.ZGVm", "description": "v1.eGl6.enp6",
        })
        assert r.status_code == 200

        with Session(engine) as sess:
            from sqlmodel import select
            remaining = sess.exec(
                select(DBMemoryTranslation).where(DBMemoryTranslation.memory_id == mem_id)
            ).all()
        assert remaining == []

    def test_keeps_cache_when_content_stays_plaintext(self, env):
        client, engine, project_id = env
        mem_id = _insert_memory(engine, project_id, name="A place", description="Some notes")
        _insert_cached_translation(engine, mem_id)

        r = client.put(f"/api/memories/{mem_id}", json={
            "date": "2025-06-01", "geo_mode": "custom",
            "name": "A renamed place", "description": "Updated notes",
        })
        assert r.status_code == 200

        with Session(engine) as sess:
            from sqlmodel import select
            remaining = sess.exec(
                select(DBMemoryTranslation).where(DBMemoryTranslation.memory_id == mem_id)
            ).all()
        assert len(remaining) == 1

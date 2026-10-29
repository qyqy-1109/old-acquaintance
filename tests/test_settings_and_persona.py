import json
import os
import base64
import io
import struct
import tempfile
import time
import unittest
import zipfile
import zlib
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import config
from app.controllers import settings_controller
from app.models.memory import MemoryStore
from app.models.personality import Personality
from app.models.world import WorldBook
from app.services.chat_service import ChatService
from app.utils import persona_card, settings_store


CARD_V2 = {
    "spec": "chara_card_v2",
    "spec_version": "2.0",
    "data": {
        "name": "测试角色",
        "description": "一个用于测试的角色设定。",
        "personality": "冷静\n善于观察",
        "scenario": "住在一座安静的小城。",
        "first_mes": "你好呀。",
        "alternate_greetings": ["今天过得怎么样？"],
        "mes_example": "{{char}}: 示例对话",
        "system_prompt": "回答要简短。",
        "character_book": {
            "entries": [
                {"content": "测试世界的第一条设定。"},
                {"content": "这条被禁用。", "enabled": False},
            ]
        },
    },
}


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def _png_card(card: dict) -> bytes:
    payload = base64.b64encode(json.dumps(card, ensure_ascii=False).encode("utf-8"))
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", b"\x00" * 13)
            + _png_chunk(b"tEXt", b"chara\0" + payload)
            + _png_chunk(b"IEND", b""))


class PersonaCardTests(unittest.TestCase):
    def test_parse_v2_maps_persona_and_world(self):
        parsed = persona_card.parse(json.dumps(CARD_V2, ensure_ascii=False))
        self.assertEqual(parsed["format"], "chara_card_v2")
        self.assertEqual(parsed["personality"]["name"], "测试角色")
        self.assertEqual(parsed["personality"]["traits"], ["冷静", "善于观察"])
        self.assertEqual(parsed["world"], ["测试世界的第一条设定。"])
        self.assertTrue(any("示例对话" in item for item in parsed["warnings"]))

    def test_native_flat_card(self):
        parsed = persona_card.parse(json.dumps({
            "name": "原生角色",
            "identity": "来自原生格式",
            "traits": ["直接"],
            "world": ["原生世界"],
        }, ensure_ascii=False))
        self.assertEqual(parsed["format"], "old-acquaintance-flat")
        self.assertEqual(parsed["personality"]["name"], "原生角色")
        self.assertEqual(parsed["world"], ["原生世界"])

    def test_rejects_unknown_card(self):
        with self.assertRaises(persona_card.CardError):
            persona_card.parse('{"hello": "world"}')

    def test_parse_png_card_uses_chara_metadata(self):
        png = _png_card(CARD_V2)
        parsed = persona_card.parse_bytes(png, "card.png")
        self.assertEqual(parsed["personality"]["name"], "测试角色")
        self.assertEqual(parsed["avatar"], png)


class PersonalityImportTests(unittest.TestCase):
    def test_import_snapshots_history_and_does_not_inherit_default_cares(self):
        parsed = persona_card.parse(json.dumps(CARD_V2, ensure_ascii=False))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "personality.json")
            p = Personality(path=path)
            old_version = p.data["version"]
            message = p.import_persona(parsed["personality"])
            self.assertIn("测试角色", message)
            self.assertEqual(p.display_name(), "测试角色")
            self.assertEqual(p.data["cares"], [])
            self.assertEqual(len(p.history), 1)
            self.assertEqual(p.data["version"], old_version + 1)
            again = Personality(path=path)
            self.assertEqual(again.display_name(), "测试角色")
            self.assertEqual(again.history[0]["reason"], "导入人格卡片")

    def test_export_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Personality(path=os.path.join(d, "personality.json"))
            exported = p.export_current()
            self.assertEqual(exported["name"], p.display_name())


class WorldImportTests(unittest.TestCase):
    class Store:
        def __init__(self):
            self.on_world = None
            self.on_drop = None

        def world_texts(self):
            return []

        def sync_world(self, texts):
            return 0, 0

    def test_bulk_import_deduplicates_without_sync(self):
        with tempfile.TemporaryDirectory() as d:
            world = WorldBook(self.Store(), path=os.path.join(d, "world.json"))
            note = world.import_entries(["一", "一", "二"], sync=False)
            self.assertIn("2 条", note)
            self.assertEqual(world.count(), 2)


class SettingsStoreTests(unittest.TestCase):
    def test_save_load_mask_and_clear(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            settings_store.save(path, {"WB_API_KEY": "sk-1234567890", "WB_MODEL": "m"})
            loaded = settings_store.load(path)
            self.assertEqual(loaded["WB_MODEL"], "m")
            self.assertEqual(settings_store.mask("sk-1234567890"), "sk-1…7890")
            settings_store.save(path, {}, clear=["WB_API_KEY"])
            self.assertNotIn("WB_API_KEY", settings_store.load(path))

    def test_validate_rejects_bad_url(self):
        with self.assertRaises(Exception):
            settings_controller._validate({"WB_BASE_URL": "ftp://example.com"}, [])


class NoKeyStartupTests(unittest.TestCase):
    def _patches(self, stack, directory):
        for name, value in {
            "DATA_DIR": directory,
            "API_KEY": "",
            "CHAT_API_KEY": "",
            "EMBED_API_KEY": "",
            "CHECK_ON_START": False,
            "AMAP_KEY": "",
            "AMAP_CITY": "",
            "AUTO_EMOTION": False,
            "AUTO_EVOLVE_PERSONALITY": False,
            "TTS_ENABLED": False,
        }.items():
            stack.enter_context(patch.object(config, name, value))

    def test_service_boots_and_guards_model_paths(self):
        with tempfile.TemporaryDirectory() as d:
            with ExitStack() as stack:
                self._patches(stack, d)
                service = ChatService()
                try:
                    self.assertFalse(service.keys_ready)
                    with self.assertRaises(RuntimeError):
                        service.prepare("你好")
                    greeting = service.greeting(force=True)
                    self.assertIsInstance(greeting, str)
                finally:
                    service.close()

    def test_test_connection_reports_missing_keys(self):
        with ExitStack() as stack:
            for name in ("API_KEY", "CHAT_API_KEY", "EMBED_API_KEY"):
                stack.enter_context(patch.object(config, name, ""))
            stack.enter_context(patch.dict(os.environ, {
                "WB_API_KEY": "", "WB_CHAT_API_KEY": "", "WB_EMBED_API_KEY": "",
            }, clear=False))
            result = settings_controller._test_connection({})
            self.assertFalse(result["chat"]["ok"])
            self.assertFalse(result["embedding"]["ok"])


class MessageStoreTests(unittest.TestCase):
    def test_edit_delete_and_regenerate_pair(self):
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(SimpleNamespace(), path=os.path.join(d, "memory.db"),
                                embed_model="offline-test")
            try:
                store.sessions["s"] = [
                    {"role": "user", "content": "你好", "ts": 1.0},
                    {"role": "assistant", "content": "你好呀", "ts": 2.0},
                ]
                store._synced["s"] = 0
                store.save()
                items = {item["id"]: item for item in store.list_sessions()}
                self.assertEqual(items["s"]["title"], "你好")
                store.rename_session("s", "自定义标题")
                store.sessions["s"].append({"role": "user", "content": "新消息", "ts": 3.0})
                store.save()
                items = {item["id"]: item for item in store.list_sessions()}
                self.assertEqual(items["s"]["title"], "自定义标题")
                store.sessions["s"].pop()
                store.save()
                question, removed = store.pop_last_turn("s")
                self.assertEqual(question, "你好")
                self.assertEqual(store.session_messages("s"), [])
                store.restore_turn("s", removed)
                self.assertEqual(len(store.session_messages("s")), 2)
                store.edit_message("s", 1, "改过的回复")
                self.assertEqual(store.session_messages("s")[1]["content"], "改过的回复")
                store.delete_message("s", 0)
                self.assertEqual([m["role"] for m in store.session_messages("s")],
                                 ["assistant"])
            finally:
                store.db.close()


class ApiSmokeTests(unittest.TestCase):
    def test_health_settings_persona_and_chat_guard(self):
        with tempfile.TemporaryDirectory() as d:
            with ExitStack() as stack:
                for name, value in {
                    "DATA_DIR": d,
                    "API_KEY": "",
                    "CHAT_API_KEY": "",
                    "EMBED_API_KEY": "",
                    "CHECK_ON_START": False,
                    "AMAP_KEY": "",
                    "AMAP_CITY": "",
                    "AUTO_EMOTION": False,
                    "AUTO_EVOLVE_PERSONALITY": False,
                    "TTS_ENABLED": False,
                }.items():
                    stack.enter_context(patch.object(config, name, value))
                from fastapi.testclient import TestClient
                from app.main import app
                from app.services import registry

                registry.close_all()
                with TestClient(app, client=("127.0.0.1", 50000)) as client:
                    health = client.get("/api/healthz").json()
                    self.assertFalse(health["keys_ready"])
                    self.assertTrue(health["first_run"])
                    self.assertTrue(health["data_dir_writable"])
                    self.assertNotIn("data_dir", health)
                    settings = client.get("/api/settings")
                    self.assertEqual(settings.status_code, 200)
                    card = json.dumps(CARD_V2, ensure_ascii=False)
                    preview = client.post("/api/persona/preview",
                                          json={"filename": "card.json", "content": card})
                    self.assertEqual(preview.status_code, 200)
                    self.assertEqual(preview.json()["name"], "测试角色")
                    imported = client.post("/api/persona/import",
                                            json={"filename": "card.json", "content": card})
                    self.assertEqual(imported.status_code, 200)
                    self.assertEqual(client.get("/api/state").json()["name"], "测试角色")
                    exported = client.get("/api/persona/export?format=v2")
                    self.assertEqual(exported.status_code, 200)
                    self.assertEqual(exported.json()["spec"], "chara_card_v2")
                    png = _png_card(CARD_V2)
                    png_import = client.post("/api/persona/import", json={
                        "filename": "card.png",
                        "content_base64": base64.b64encode(png).decode("ascii"),
                    })
                    self.assertEqual(png_import.status_code, 200)
                    av = client.get("/api/persona/avatar")
                    self.assertEqual(av.status_code, 200)
                    self.assertEqual(av.headers["content-type"], "image/png")
                    self.assertEqual(av.content, png)
                    self.assertEqual(client.post("/api/chat", json={"message": "hi"}).status_code, 503)
                    created = client.post("/api/sessions", json={"title": "第二段"})
                    self.assertEqual(created.status_code, 200)
                    session_id = created.json()["current"]
                    listed = client.get("/api/sessions").json()
                    item = next(i for i in listed["items"] if i["id"] == session_id)
                    self.assertEqual(item["title"], "第二段")
                    renamed = client.patch(f"/api/sessions/{session_id}",
                                           json={"title": "改过的名字"})
                    self.assertEqual(renamed.status_code, 200)
                    listed = client.get("/api/sessions").json()
                    item = next(i for i in listed["items"] if i["id"] == session_id)
                    self.assertEqual(item["title"], "改过的名字")
                    self.assertEqual(client.post(f"/api/sessions/{session_id}/select",
                                                 json={}).status_code, 200)
                    self.assertEqual(client.get(f"/api/sessions/{session_id}/messages")
                                     .json()["messages"], [])
                    self.assertEqual(client.post(f"/api/sessions/{session_id}/messages")
                                     .status_code, 405)
                    exported = client.get("/api/export")
                    self.assertEqual(exported.status_code, 200)
                    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
                        self.assertIn("memory.db", archive.namelist())
                        self.assertNotIn("settings.json", archive.namelist())
                    self.assertEqual(client.delete(f"/api/sessions/{session_id}").status_code, 200)
                    service = registry.get("")
                    with service.store._lock:
                        service.store.sessions.setdefault("default", []).append({
                            "role": "assistant", "content": "测试问候",
                            "ts": time.time(), "greeting": True,
                        })
                    service.store.save()
                    messages = client.get("/api/sessions/default/messages").json()["messages"]
                    self.assertTrue(messages)
                    edited = client.patch("/api/sessions/default/messages/0",
                                          json={"content": "改过的问候"})
                    self.assertEqual(edited.status_code, 200)
                    self.assertEqual(edited.json()["messages"][0]["content"], "改过的问候")
                    deleted = client.delete("/api/sessions/default/messages/0")
                    self.assertEqual(deleted.status_code, 200)
                    self.assertEqual(deleted.json()["messages"], [])
                    found = client.get("/api/search", params={"q": "测试"})
                    self.assertEqual(found.status_code, 200)
                    self.assertTrue(found.json()["world"])
                registry.close_all()


if __name__ == "__main__":
    unittest.main()

"""时间轴与状态报告使用请求中的名字，历史数据保持原样。"""

from __future__ import annotations

import json
import shutil
import struct
import sys
import unittest
from collections import OrderedDict
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config
from app.controllers.chat_controller import router
from app.models.memory import MemoryStore
from app.services import auth, registry
from app.services.chat_service import ChatService


class MemoryDisplayNamesTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.directory = (Path(__file__).resolve().parents[1] /
                          "_tmptest/state-display-200-201/cases" / self._testMethodName)
        if self.directory.exists():
            shutil.rmtree(self.directory)
        self.directory.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(self.directory))
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(config, "DATA_DIR", str(self.directory)))
        self.stack.enter_context(patch.object(config, "AUTH_REQUIRED", False))
        for target in ("requests.sessions.Session.request", "urllib.request.urlopen"):
            self.stack.enter_context(patch(target, side_effect=AssertionError("测试禁止联网")))
        self.store = MemoryStore(SimpleNamespace(), path=str(self.directory / "memory.db"),
                                 embed_model="test-embedding")
        self.stack.callback(self.store.db.close)
        with self.store.db:
            for text, date, actor, stamp, archived in (
                ("用户和助手在书店见面", "2026-10-01", "user", 100, None),
                ("用户：给机器人带了书", "2026-10-01", "user", 100, None),
                ("AI提醒用户带伞", "2026-09-30", "assistant", 90, None),
                ("用户的旧位置在广场，助手在那里", "2026-09-29", "user", 80, 110),
            ):
                self.store.db.execute(
                    "INSERT INTO memories(text,kind,ts,session,event_date,actor,vec,archived_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (text, "fact", stamp, "default", date, actor, struct.pack("f", 1), archived))
            self.store.db.executemany(
                "INSERT INTO slots(entity,attr,value,updated_at) VALUES(?,?,?,?)",
                [("用户", "位置", "用户体验中心，等助手和用户", 100),
                 ("助手", "状态", "AI在家整理用户手册", 99),
                 ("年糕", "状态", "用户的猫；用户名、多用户保持原文", 98)])
        self.store._load()
        self.service = object.__new__(ChatService)
        self.service.store = self.store
        self.service.personality = SimpleNamespace(display_name=lambda: "沈知夏")
        self.service.user_id = ""
        self.stack.enter_context(patch.object(registry, "_services", OrderedDict({"": self.service})))
        self.stack.enter_context(patch.object(registry, "_refs", {}))
        self.app = FastAPI()
        self.app.include_router(router)
        self.app.state.registry = registry
        self.app.state.service = self.service
        self.client = TestClient(self.app)
        self.stack.callback(self.client.close)

    def post(self, command, headers=None, **extra):
        response = self.client.post("/api/command", json={"command": command, **extra}, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["output"]

    def bind_token(self, name):
        registry._services[name] = self.service
        return {"Authorization": f"Bearer {auth.issue(name)}"}

    def snapshot(self):
        self.store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return ((self.directory / "memory.db").read_bytes(),
                json.dumps([self.store.memories, self.store.archived], ensure_ascii=False, sort_keys=True))

    def test_token_names_stay_separate_on_shared_service_and_win_over_header_and_body(self):
        before = self.snapshot()
        for name in ("小林", "阿明", "小林"):
            headers = self.bind_token(name)
            headers["X-User-Id"] = "forged-name"
            for command in ("/timeline", "/时间轴", "/slots", "/状态"):
                with self.subTest(name=name, command=command):
                    output = self.post(command, headers, user_name="body-name")
                    self.assertIn(name, output)
                    self.assertNotIn("forged-name", output)
                    self.assertNotIn("body-name", output)
                    self.assertNotIn("阿明" if name == "小林" else "小林", output)
        self.assertEqual(self.service.user_id, "")
        self.assertEqual(self.snapshot(), before)

    def test_claimed_root_account_displays_login_name_with_empty_storage_uid(self):
        headers = self.bind_token("认领名字")
        connection = auth._connect()
        try:
            with connection:
                connection.execute("INSERT INTO accounts VALUES(?,?,?,?)", ("认领名字", "test-hash", "", 1))
        finally:
            connection.close()
        self.assertEqual(auth.storage_uid("认领名字"), "")
        self.assertIn("认领名字", self.post("/timeline", headers))
        self.assertIn("认领名字·位置", self.post("/slots", headers))
        self.assertEqual(self.service.user_id, "")

    def test_percent_encoded_nickname_is_decoded_before_display(self):
        registry._services["小用户AI"] = self.service
        headers = {"X-User-Id": quote("小用户AI"), "X-User-Id-Encoding": "percent-encoded"}
        for command in ("/timeline", "/slots"):
            with self.subTest(command=command):
                output = self.post(command, headers)
                self.assertIn("小用户AI", output)
                self.assertNotIn("%E5", output)
                self.assertNotIn("小小用户", output)
                self.assertNotIn("小用户沈知夏", output)

    def test_timeline_maps_folded_and_archived_text_without_changing_originals(self):
        before = self.snapshot()
        output = self.post("/timeline", self.bind_token("小林"))
        self.assertIn("小林和沈知夏在书店见面", output)
        self.assertIn("小林：给沈知夏带了书", output)
        self.assertIn("沈知夏提醒小林带伞", output)
        self.assertIn("小林的旧位置在广场，沈知夏在那里", output)
        self.assertIn("└", output)
        self.assertIn("这次共 2 条", output)
        self.assertIn("已归档", output)
        self.assertEqual(self.snapshot(), before)

    def test_slots_map_entity_and_value_but_keep_compound_words_and_original_rows(self):
        before = self.snapshot()
        original_slots = self.store.slots_all()
        output = self.post("/slots", self.bind_token("小林"))
        self.assertIn("小林·位置：用户体验中心，等沈知夏和小林", output)
        self.assertIn("沈知夏·状态：沈知夏在家整理用户手册", output)
        self.assertIn("年糕·状态：小林的猫；用户名、多用户保持原文", output)
        self.assertEqual(self.store.slots_all(), original_slots)
        self.assertEqual(self.snapshot(), before)

    def test_cli_uses_instance_name_and_default_or_anonymous_request_uses_you(self):
        self.service.user_id = "命令行名字"
        self.assertIn("命令行名字和沈知夏", self.service.run_command("/timeline"))
        self.assertIn("命令行名字·位置", self.service.run_command("/slots"))
        self.service.user_id = ""
        self.assertIn("你和沈知夏", self.post("/timeline"))
        self.assertIn("你和沈知夏", self.service.timeline_report())
        self.assertIn("你·位置", self.service.run_command("/slots"))

    def test_existing_real_names_with_role_words_are_preserved_in_both_reports(self):
        headers = self.bind_token("小用户AI")
        for her_name in ("AI助手澜舒", "机器人澜舒"):
            self.service.personality = SimpleNamespace(display_name=lambda: her_name)
            original = f"小用户AI和{her_name}约好，用户给助手带书"
            with self.store.db:
                self.store.db.execute("UPDATE memories SET text=? WHERE id=1", (original,))
                self.store.db.execute("DELETE FROM slots")
                self.store.db.execute("INSERT INTO slots VALUES(?,?,?,?)",
                                      ("小用户AI", "状态", original, 100))
            self.store._load()
            before = self.snapshot()
            for command in ("/timeline", "/slots"):
                with self.subTest(her_name=her_name, command=command):
                    output = self.post(command, headers)
                    self.assertIn(f"小用户AI和{her_name}约好，小用户AI给{her_name}带书", output)
                    if command == "/slots":
                        self.assertIn("小用户AI·状态", output)
            self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()

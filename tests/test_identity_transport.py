"""Nickname transport regressions, without model calls or new dependencies."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import config
from app.controllers.chat_controller import svc
from app.main import HoldUserServiceMiddleware
from app.services import auth, registry


class TestIdentityTransport(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.data_dir.cleanup)
        self.enterContext(patch.object(config, "DATA_DIR", self.data_dir.name))
        self.enterContext(patch.object(config, "AUTH_REQUIRED", False))
        self.active = {}

        def acquire(uid):
            self.active[uid] = self.active.get(uid, 0) + 1
            return uid

        def release(uid):
            self.active[uid] -= 1
            if not self.active[uid]:
                del self.active[uid]

        def get(uid):
            return SimpleNamespace(user_id=uid, held=self.active.get(uid, 0) > 0)

        self.enterContext(patch.object(registry, "acquire", acquire))
        self.enterContext(patch.object(registry, "release", release))
        app = FastAPI()
        app.add_middleware(HoldUserServiceMiddleware)
        app.state.registry = SimpleNamespace(get=get)
        app.state.service = SimpleNamespace(user_id="", held=False)

        @app.get("/api/identity")
        def identity(request: Request):
            service = svc(request)
            return {"uid": service.user_id, "held": service.held}

        self.client = self.enterContext(TestClient(app))

    def assert_identity(self, headers, uid):
        response = self.client.get("/api/identity", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"uid": uid, "held": True})
        self.assertEqual(self.active, {}, "request must release its user instance")

    def test_encoded_nickname_is_restored_for_controller_and_middleware(self):
        self.assert_identity(
            {"X-User-Id": "%E5%A5%B6%E9%BE%99",
             "X-User-Id-Encoding": "percent-encoded"},
            "\u5976\u9f99",
        )

    def test_legacy_ascii_escape_is_kept_verbatim(self):
        """不带标记 + 解出来是 ASCII → 按原值（真昵称里完全可能带百分号）。

        ⚠ 这条契约在 `#95`（T70-②）被收窄过：原来「未带标记一律按原值」，
        实测导致同一个中文昵称分裂成两个数据目录（`users/小林` 与
        `users/%E5%A5%B6%E9%BE%99` 并存），所以改成"解出来是非 ASCII 才还原"。
        ASCII 转义这条继续守住——它才是"真名带百分号"的那一类。
        """
        self.assert_identity({"X-User-Id": "%41%42"}, "%41%42")

    def test_legacy_cjk_escape_is_restored(self):
        """不带标记、但解出来是中文 → 还原（这正是实测分裂成两个目录的那种请求）。"""
        self.assert_identity({"X-User-Id": "%E5%A5%B6%E9%BE%99"}, "\u5976\u9f99")

    def test_encoded_percent_nickname_is_decoded_only_once(self):
        self.assert_identity(
            {"X-User-Id": "%25E5%25A5%25B6",
             "X-User-Id-Encoding": "percent-encoded"},
            "%E5%A5%B6",
        )

    def test_invalid_utf8_does_not_select_a_user(self):
        response = self.client.get("/api/identity", headers={
            "X-User-Id": "%FF", "X-User-Id-Encoding": "percent-encoded",
        })
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.active, {})

    def test_token_still_has_priority_and_required_auth_rejects_nicknames(self):
        token = auth.issue("token-user")
        nickname = {"X-User-Id": "%E5%A5%B6%E9%BE%99",
                    "X-User-Id-Encoding": "percent-encoded"}
        with patch.object(config, "AUTH_REQUIRED", True):
            self.assert_identity({**nickname, "Authorization": "Bearer " + token},
                                 "token-user")
            for extra in ({}, {"Authorization": "Bearer invalid-token"}):
                response = self.client.get("/api/identity", headers={**nickname, **extra})
                self.assertEqual(response.status_code, 401, response.text)
                self.assertEqual(self.active, {})

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed to run browser helpers")
    def test_real_browser_helpers_send_nicknames_that_round_trip(self):
        api_module = Path(__file__).parents[1] / "app/views/js/api.js"
        result = subprocess.run(
            [shutil.which("node"), "-e", r'''
(async () => {
  const {ApiClient} = await import(require("node:url").pathToFileURL(process.argv[1]).href);
  const context = new ApiClient({storage: {getItem: () => null},
    fetchImpl: async (url, options) => ({ok: true, status: 200, url,
      headers: Object.fromEntries(new Headers(options.headers))})});
  const output = [];
  for (const name of ["\u5976\u9f99", "M\u00e9lanie", "\u5976\u9f99\ud83c\udf1f",
                      "A+B 100%", "%E5%A5%B6%E9%BE%99", "frontend-check"]) {
    context.setIdentity("nickname", name);
    const request = await context.request("/api/state", {headers: {Accept: "application/json"}});
    output.push({name, headers: request.headers});
  }
  context.setIdentity("token", "test-token");
  output.push(await context.request("/api/state"));
  console.log(JSON.stringify(output));
})().catch(error => {console.error(error); process.exitCode = 1;});
''', str(api_module)],
            text=True, encoding="utf-8", capture_output=True,
            timeout=15, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        requests = json.loads(result.stdout)
        for request in requests[:-1]:
            with self.subTest(nickname=request["name"]):
                self.assertEqual(request["headers"]["accept"], "application/json")
                self.assert_identity(request["headers"], request["name"])
        self.assertEqual(requests[-1]["headers"], {"authorization": "Bearer test-token"})


if __name__ == "__main__":
    unittest.main(verbosity=2)

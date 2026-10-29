"""Contract tests for the isolated natural-companionship evaluator."""

from __future__ import annotations

import json
import datetime as dt
import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmark.evaluate_companionship import (
    build_blind_order,
    canonical_hash,
    build_messages,
    build_fixture_bundle,
    load_cases,
    ModelRecorder,
    recover_entries,
    sanitize_model_params,
    validate_cases,
)
from benchmark import evaluate_companionship as evaluator
from app.models import db as memory_db
from app.models.life import LifeBook


class CompanionshipEvaluationTests(unittest.TestCase):
    def test_fixed_cases_cover_twelve_three_turn_groups(self):
        cases = load_cases()
        self.assertEqual(len(cases), 12)
        self.assertEqual({case["id"] for case in cases}, {case["group"] for case in cases})
        self.assertTrue(all(len(case["turns"]) == 3 for case in cases))
        self.assertTrue(all(all(turn["input"].strip() for turn in case["turns"])
                            for case in cases))
        validate_cases(cases)

    def test_hash_is_canonical_and_changes_when_output_changes(self):
        payload = {"input": [{"role": "user", "content": "你好"}],
                   "params": {"model": "test", "temperature": 0.7},
                   "output": "回应"}
        self.assertEqual(canonical_hash(payload), canonical_hash(json.loads(json.dumps(payload))))
        changed = dict(payload, output="另一句回应")
        self.assertNotEqual(canonical_hash(payload), canonical_hash(changed))
        self.assertEqual(len(canonical_hash(payload)), 64)

    def test_model_params_are_redacted_without_losing_request_shape(self):
        params = {"model": "mimo", "temperature": 0.7, "max_tokens": 512,
                  "api_key": "secret", "headers": {"Authorization": "Bearer secret"},
                  "extra_body": {"thinking": {"type": "disabled"}}}
        clean = sanitize_model_params(params)
        self.assertEqual(clean["model"], "mimo")
        self.assertEqual(clean["max_tokens"], 512)
        self.assertEqual(clean["extra_body"], params["extra_body"])
        self.assertNotIn("api_key", json.dumps(clean).lower())
        self.assertNotIn("authorization", json.dumps(clean).lower())

    def test_blind_order_is_reproducible_and_hides_version(self):
        entries = [{"group": "倾诉", "id": "倾诉", "version": version, "round": turn}
                   for version in ("before", "current") for turn in (1, 2, 3)]
        first = build_blind_order(entries, seed=193196)
        second = build_blind_order(entries, seed=193196)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 6)
        self.assertTrue(all(item["blind_id"].startswith("blind-") for item in first))
        self.assertTrue(all("version" not in item for item in first))
        self.assertTrue(all("item_id" in item and "before" not in item["item_id"]
                            and "current" not in item["item_id"] for item in first))
        self.assertEqual(len({item["item_id"] for item in first}), len(entries))
        self.assertTrue(all(not item["item_id"].removeprefix("item-").isdigit()
                            for item in first), "盲评 id 不得泄露版本在原数组中的前后位置")

    def test_offline_recorder_captures_exact_request_without_dispatch(self):
        class ForbiddenClient:
            @property
            def chat(self):
                raise AssertionError("offline mode dispatched a client call")

        recorder = ModelRecorder(ForbiddenClient(), live=False)
        response = recorder.chat.completions.with_raw_response.create(
            model="fixture", messages=[{"role": "user", "content": "原始输入"}],
            temperature=0.7, max_tokens=123,
            extra_body={"thinking": {"type": "disabled"}})
        self.assertEqual(response.parse().choices, [])
        self.assertEqual(recorder.calls[0]["model_params"]["max_tokens"], 123)
        self.assertEqual(recorder.calls[0]["input"][0]["content"], "原始输入")
        self.assertIsNone(recorder.calls[0]["raw_output"])
        self.assertEqual(recorder.calls[0]["error"]["type"], "offline")

    def test_live_recorder_retains_raw_response_and_all_call_parameters(self):
        def create(**kwargs):
            return SimpleNamespace(
                text='{"choices":[{"message":{"content":"原始回答"}}]}',
                status_code=200,
                parse=lambda: SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content="原始回答"))]))

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            with_raw_response=SimpleNamespace(create=create))))
        recorder = ModelRecorder(client, live=True)
        recorder.chat.completions.with_raw_response.create(
            model="fixture", temperature=0.9, max_tokens=200,
            messages=[{"role": "user", "content": "一次请求"}],
            extra_body={"thinking": {"type": "disabled"}})
        call = recorder.calls[0]
        self.assertEqual(call["raw_output"], "原始回答")
        self.assertIn('"choices"', call["raw_response"])
        self.assertEqual(call["status_code"], 200)
        self.assertEqual(call["model_params"]["extra_body"],
                         {"thinking": {"type": "disabled"}})

    def test_context_keeps_prior_user_inputs_when_model_was_skipped(self):
        history = [{"role": "user", "content": "第一轮未完事"}]
        result = build_messages("隔离系统", history, "第二轮还没完")
        self.assertEqual(result, [{"role": "system", "content": "隔离系统"},
                                  *history, {"role": "user", "content": "第二轮还没完"}])
        self.assertEqual(history, [{"role": "user", "content": "第一轮未完事"}])

    def test_schema_rejects_fixed_answers_and_duplicate_case_ids(self):
        cases = load_cases()
        cases[0]["expected_output"] = "必须这样回答"
        with self.assertRaisesRegex(ValueError, "固定答案"):
            validate_cases(cases)
        del cases[0]["expected_output"]
        cases[1]["id"] = cases[0]["id"]
        with self.assertRaisesRegex(ValueError, "重复"):
            validate_cases(cases)

    def test_fixture_copies_complete_personality_and_sets_familiar_relationship(self):
        personality = {"name": "沈知夏", "behavior": ["原模板行为"],
                       "scope": {"can": ["日常闲聊"]}, "favorite_places": ["学校球场"]}
        relationship = {"stage": "", "decide": "auto", "care_style": "模板关心方式",
                        "stages": [{"key": "熟络", "call_him": "阿哲"}]}
        bundle = build_fixture_bundle(personality, relationship)
        self.assertEqual(bundle["personality"], personality)
        self.assertEqual(bundle["relationship"]["stage"], "熟络")
        self.assertEqual(bundle["relationship"]["decide"], "she")
        self.assertEqual(bundle["relationship"]["care_style"], "模板关心方式")
        self.assertEqual(bundle["personality_hash"], canonical_hash(personality))
        self.assertEqual(bundle["relationship_hash"], canonical_hash(bundle["relationship"]))
        bundle["personality"]["behavior"].append("隔离改变")
        bundle["relationship"]["stages"][0]["call_him"] = "隔离称呼"
        self.assertEqual(personality["behavior"], ["原模板行为"])
        self.assertEqual(relationship["stages"][0]["call_him"], "阿哲")

    def test_recovery_keeps_completed_outputs_and_marks_each_missing_turn(self):
        cases = load_cases()
        first = {"id": cases[0]["id"], "group": cases[0]["group"], "round": 1,
                 "input": [{"role": "user", "content": "已完成输入"}],
                 "raw_output": "已完成原始回答", "error": None}
        error = {"type": "worker_timeout", "message": "worker exceeded timeout"}
        recovered = recover_entries(cases, [first], error)
        self.assertEqual(len(recovered), 36)
        self.assertEqual(recovered[0], first)
        self.assertEqual(recovered[1]["error"], error)
        self.assertEqual(recovered[1]["input"][-1]["content"], cases[0]["turns"][1]["input"])
        self.assertIn("已完成原始回答", json.dumps(recovered[1]["input"], ensure_ascii=False))
        self.assertTrue(all(entry.get("raw_output") is None for entry in recovered[1:]))
        self.assertEqual(recovered[-1]["input"][-1]["content"], cases[-1]["turns"][-1]["input"])

    def test_fixture_life_event_is_persisted_with_explicit_provenance(self):
        root = Path("_tmptest/natural-193-196") / f"fixture-test-{uuid.uuid4().hex}"
        root.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        connection = memory_db.connect(str(root / "memory.db"))
        self.addCleanup(connection.close)
        life = LifeBook(connection, path=str(root / "life.json"), her_name="隔离她")
        when = dt.datetime(2026, 10, 15, 15, 58)
        seed = getattr(evaluator, "seed_fixture_life_event", None)
        self.assertTrue(callable(seed), "生活评测夹具必须写入可核验的 life_event 来源")
        row = seed(
            life,
            when=when,
            text="隔离夹具：她刚从楼下书店回来",
            source="model",
        )
        self.assertEqual(row["source"], "model")
        self.assertEqual(row["text"], "隔离夹具：她刚从楼下书店回来")
        self.assertEqual(row["ts"], when.timestamp())
        self.assertIsNone(row["hidden_at"])
        persisted = life.recent_events(limit=10)
        self.assertEqual(persisted[0]["rid"], row["rid"])
        self.assertEqual(persisted[0]["source"], "model")

    def test_both_workers_capture_the_same_source_backed_life_fixture(self):
        snapshots = (evaluator.SNAPSHOT_DIR, evaluator.ROOT)
        fixture_rows = []
        for snapshot in snapshots:
            result = subprocess.run(
                [sys.executable, "-X", "utf8", str(Path(evaluator.__file__)), "--worker",
                 "--snapshot", str(snapshot)],
                cwd=evaluator.ROOT, capture_output=True, text=True, encoding="utf-8",
                timeout=90,
            )
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-1000:])
            marker = next(line[len(evaluator.WORKER_MARKER):]
                          for line in result.stdout.splitlines()
                          if line.startswith(evaluator.WORKER_MARKER))
            entries = json.loads(marker)["entries"]
            self.assertEqual(len(entries), 36)
            for entry in entries:
                fixture = entry["fixture"]
                self.assertIn("life_event", fixture,
                              "worker 不能只有 life_state，必须保留实际生活来源")
                event = fixture["life_event"]
                self.assertEqual(fixture["life_event_origin"], "synthetic_isolated_fixture")
                self.assertEqual(event["source"], "model")
                self.assertLess(event["ts"], dt.datetime.fromisoformat(entry["fixed_time"]).timestamp())
                self.assertIsNone(event["hidden_at"])
                self.assertTrue(event["text"])
                self.assertTrue(event["rid"])
                self.assertEqual(entry["isolation"]["history_mode"], "explicit_history_no_extraction")
                self.assertEqual(entry["isolation"]["blocked_network"], [])
                fixture_rows.append(event)
        self.assertTrue(all(row == fixture_rows[0] for row in fixture_rows))


if __name__ == "__main__":
    unittest.main()

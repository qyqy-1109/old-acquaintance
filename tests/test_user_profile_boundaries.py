"""Regression tests for user-profile source and correction boundaries."""

import unittest
import uuid
import shutil
from pathlib import Path
from unittest.mock import patch

from app.models.user import UserProfile


TEST_TMP_ROOT = Path(__file__).parents[1] / "_tmptest"


class UserProfileBoundaryTests(unittest.TestCase):
    def test_correction_handles_whitespace_in_existing_fact(self):
        profile = UserProfile(None, path=str(TEST_TMP_ROOT / f"profile-{uuid.uuid4().hex}.json"))
        profile.data["facts"] = ["用户住在杭州 ", "用户养了一只狗"]
        response = {"facts": ["用户住在成都"], "corrections": [{
            "field": "facts", "old": "用户住在杭州 ", "new": "用户住在成都",
            "quote": "我搬到成都了",
        }]}
        with patch("app.models.user.chat_json", return_value=(response, "")), patch.object(profile, "_save"):
            profile.analyze([{"role": "user", "content": "我搬到成都了"}])
        self.assertNotIn("杭州", profile.render())
        self.assertIn("用户养了一只狗", profile.data["facts"])
        self.assertEqual(profile.data["history"][0]["old"], "用户住在杭州 ")

    def test_assistant_self_description_never_enters_user_profile(self):
        """Assistant turns must not be treated as evidence about the user."""
        captured = []

        def fake_chat_json(_client, _model, messages, **_kwargs):
            prompt = messages[0]["content"]
            captured.append(prompt)
            if "assistant:" in prompt:
                return {
                    "summary": "用户叫沈知夏，住在成都",
                    "facts": ["用户叫沈知夏", "用户住在成都"],
                }, ""
            return {
                "summary": "用户叫小林",
                "facts": ["用户叫小林"],
            }, ""

        path = TEST_TMP_ROOT / f"profile-{uuid.uuid4().hex}.json"
        profile = UserProfile(None, path=str(path))
        turns = [
            {"role": "user", "content": "我叫小林"},
            {"role": "assistant", "content": "我叫沈知夏，我住在成都"},
        ]
        with patch("app.models.user.chat_json", side_effect=fake_chat_json), \
                patch.object(profile, "_save"):
            profile.analyze(turns)

        self.assertTrue(captured)
        self.assertNotIn("assistant: 我叫沈知夏，我住在成都", captured[0])
        self.assertEqual(profile.data["summary"], "用户叫小林")
        self.assertEqual(profile.data["facts"], ["用户叫小林"])

    def test_explicit_user_correction_removes_old_profile_fact(self):
        """A direct correction must stop the previous fact being current."""
        def fake_chat_json(_client, _model, _messages, **_kwargs):
            return {
                "summary": "用户已经搬到成都",
                "facts": ["用户住在成都"],
                "corrections": [{
                    "field": "facts",
                    "old": "用户住在杭州",
                    "new": "用户住在成都",
                    "quote": "我已经搬到成都了，不住杭州",
                }],
            }, ""

        path = TEST_TMP_ROOT / f"profile-{uuid.uuid4().hex}.json"
        profile = UserProfile(None, path=str(path))
        profile.data.update({
            "summary": "用户住在杭州",
            "facts": ["用户住在杭州", "用户养了一只狗"],
            "version": 1,
        })
        with patch("app.models.user.chat_json", side_effect=fake_chat_json), \
                patch.object(profile, "_save"):
            profile.analyze([{
                "role": "user",
                "content": "我已经搬到成都了，不住杭州",
            }])

        self.assertEqual(profile.data["summary"], "用户养了一只狗；用户住在成都")
        self.assertNotIn("用户住在杭州", profile.data["facts"])
        self.assertIn("用户住在成都", profile.data["facts"])
        self.assertIn("用户养了一只狗", profile.data["facts"])
        self.assertNotIn("corrections", profile.data)
        history = profile.data["history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["field"], "facts")
        self.assertEqual(history[0]["old"], "用户住在杭州")
        self.assertEqual(history[0]["new"], "用户住在成都")
        self.assertEqual(history[0]["quote"], "我已经搬到成都了，不住杭州")
        self.assertGreater(history[0]["ts"], 0)

    def test_only_assistant_turns_do_not_update_or_call_model(self):
        path = TEST_TMP_ROOT / f"profile-{uuid.uuid4().hex}.json"
        profile = UserProfile(None, path=str(path))
        before = dict(profile.data)
        with patch("app.models.user.chat_json", return_value=({}, "")) as analyze, \
                patch.object(profile, "_save") as save:
            result = profile.analyze([
                {"role": "assistant", "content": "我刚从医院回来"},
                {"role": "system", "content": "用户在杭州"},
            ])
        self.assertEqual(result, "")
        self.assertEqual(profile.data, before)
        analyze.assert_not_called()
        save.assert_not_called()

    def test_invalid_corrections_keep_old_fact_and_do_not_add_history(self):
        invalid = [
            {"field": "facts", "old": "用户住在杭州", "new": "用户住在成都",
             "quote": "我已经搬到成都了"},
            {"field": "facts", "old": "用户住在杭州", "new": "", "quote": "今天下雨了"},
            {"field": "facts", "old": "用户住杭州", "new": "用户住在成都",
             "quote": "今天下雨了"},
            {"field": "summary", "old": "用户住在杭州", "new": "用户住在成都",
             "quote": "今天下雨了"},
        ]
        for correction in invalid:
            with self.subTest(correction=correction):
                path = TEST_TMP_ROOT / f"profile-{uuid.uuid4().hex}.json"
                profile = UserProfile(None, path=str(path))
                profile.data["facts"] = ["用户住在杭州", "用户养了一只狗"]
                response = {"corrections": [correction]}
                with patch("app.models.user.chat_json", return_value=(response, "")), \
                        patch.object(profile, "_save"):
                    profile.analyze([
                        {"role": "user", "content": "今天下雨了"},
                        {"role": "assistant", "content": "我已经搬到成都了"},
                    ])
                self.assertEqual(profile.data["facts"], ["用户住在杭州", "用户养了一只狗"])
                self.assertEqual(profile.data.get("history", []), [])
                self.assertNotIn("corrections", profile.data)

    def test_omitted_fields_and_uncorrected_facts_are_preserved(self):
        path = TEST_TMP_ROOT / f"profile-{uuid.uuid4().hex}.json"
        profile = UserProfile(None, path=str(path))
        profile.data.update({
            "facts": ["用户养了一只狗"],
            "preferences": ["用户喜欢简短回答"],
            "topics": ["Python"],
        })
        with patch("app.models.user.chat_json", return_value=({"facts": ["用户在学做饭"]}, "")), \
                patch.object(profile, "_save"):
            profile.analyze([{"role": "user", "content": "我最近在学做饭"}])
        self.assertEqual(profile.data["facts"], ["用户养了一只狗", "用户在学做饭"])
        self.assertEqual(profile.data["preferences"], ["用户喜欢简短回答"])
        self.assertEqual(profile.data["topics"], ["Python"])

    def test_profile_instances_do_not_share_mutable_defaults(self):
        first = UserProfile(None, path=str(TEST_TMP_ROOT / f"profile-{uuid.uuid4().hex}.json"))
        second = UserProfile(None, path=str(TEST_TMP_ROOT / f"profile-{uuid.uuid4().hex}.json"))
        for field in ("facts", "preferences", "topics", "expertise"):
            with self.subTest(field=field):
                self.assertIsNot(first.data[field], second.data[field])

    def test_correction_survives_restart_and_preserves_existing_history(self):
        directory = TEST_TMP_ROOT / f"profile-203-persistence-{uuid.uuid4().hex}"
        directory.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, directory)
        path = directory / "profile.json"
        profile = UserProfile(None, path=str(path))
        old_history = {
            "field": "facts", "old": "用户曾住北京", "new": "用户住在杭州",
            "quote": "我搬到杭州了", "ts": 1,
        }
        profile.data.update({
            "summary": "用户住在杭州",
            "facts": ["用户住在杭州", "用户养了一只狗"],
            "history": [old_history],
        })
        profile._save()
        response = {
            "summary": "用户住在成都",
            "corrections": [{
                "field": "facts", "old": "用户住在杭州", "new": "用户住在成都",
                "quote": "我搬到成都了",
            }],
            "history": [{"old": "模型伪造的历史"}],
        }
        with patch("app.models.user.chat_json", return_value=(response, "")):
            profile.analyze([{"role": "user", "content": "我搬到成都了"}])
        reloaded = UserProfile(None, path=str(path))
        self.assertEqual(reloaded.data["facts"], ["用户养了一只狗", "用户住在成都"])
        self.assertEqual(reloaded.data["history"][0], old_history)
        self.assertEqual(reloaded.data["history"][1]["old"], "用户住在杭州")
        self.assertEqual(len(reloaded.data["history"]), 2)
        self.assertNotIn("杭州", reloaded.render())
        self.assertFalse(Path(f"{path}.tmp").exists())

    def test_missing_new_summary_does_not_keep_summary_of_old_fact(self):
        profile = UserProfile(None, path=str(TEST_TMP_ROOT / f"profile-{uuid.uuid4().hex}.json"))
        profile.data.update({"summary": "用户住在杭州", "facts": ["用户住在杭州"]})
        response = {"corrections": [{
            "field": "facts", "old": "用户住在杭州", "new": "用户住在成都",
            "quote": "我搬到成都了",
        }]}
        with patch("app.models.user.chat_json", return_value=(response, "")), \
                patch.object(profile, "_save"):
            profile.analyze([{"role": "user", "content": "我搬到成都了"}])
        self.assertNotIn("杭州", profile.render())
        self.assertIn("成都", profile.render())

    def test_preference_correction_does_not_keep_old_communication_summary(self):
        profile = UserProfile(None, path=str(TEST_TMP_ROOT / f"profile-{uuid.uuid4().hex}.json"))
        profile.data.update({
            "summary": "用户希望简短交流", "preferences": ["用户喜欢简短回答"],
            "communication": "用户希望简短交流",
        })
        response = {"corrections": [{
            "field": "preferences", "old": "用户喜欢简短回答", "new": "用户希望详细说明",
            "quote": "以后详细说",
        }]}
        with patch("app.models.user.chat_json", return_value=(response, "")), \
                patch.object(profile, "_save"):
            profile.analyze([{"role": "user", "content": "以后详细说"}])
        self.assertNotIn("简短", profile.render())
        self.assertIn("用户希望详细说明", profile.render())

    def test_duplicate_corrections_archive_old_fact_once(self):
        profile = UserProfile(None, path=str(TEST_TMP_ROOT / f"profile-{uuid.uuid4().hex}.json"))
        profile.data["facts"] = ["用户住在杭州"]
        correction = {
            "field": "facts", "old": "用户住在杭州", "new": "用户住在成都",
            "quote": "我搬到成都了",
        }
        with patch("app.models.user.chat_json", return_value=({"corrections": [correction, correction]}, "")), \
                patch.object(profile, "_save"):
            profile.analyze([{"role": "user", "content": "我搬到成都了"}])
        self.assertEqual(len(profile.data["history"]), 1)

    def test_correction_quote_must_be_present_in_the_analyzed_user_slice(self):
        for turns, max_chars in [
            ([{"role": "user", "content": "甲" * 3000 + "我搬到成都了"}], 3000),
            ([{"role": "user", "content": "我搬到成都了"},
              {"role": "user", "content": "乙" * 100}], 50),
        ]:
            with self.subTest(max_chars=max_chars):
                profile = UserProfile(None, path=str(TEST_TMP_ROOT / f"profile-{uuid.uuid4().hex}.json"))
                profile.data["facts"] = ["用户住在杭州"]
                response = {"corrections": [{
                    "field": "facts", "old": "用户住在杭州", "new": "用户住在成都",
                    "quote": "我搬到成都了",
                }]}
                with patch("app.models.user.chat_json", return_value=(response, "")), \
                        patch.object(profile, "_save"):
                    profile.analyze(turns, max_chars=max_chars)
                self.assertIn("用户住在杭州", profile.data["facts"])
                self.assertEqual(profile.data.get("history", []), [])

    def test_archived_profile_facts_are_not_in_the_current_analysis_input(self):
        profile = UserProfile(None, path=str(TEST_TMP_ROOT / f"profile-{uuid.uuid4().hex}.json"))
        profile.data["facts"] = ["用户住在成都"]
        profile.data["history"] = [{
            "field": "facts", "old": "用户住在杭州", "new": "用户住在成都",
            "quote": "我搬到成都了", "ts": 1,
        }]
        captured = []

        def fake_chat_json(_client, _model, messages, **_kwargs):
            captured.append(messages[0]["content"])
            return {}, ""

        with patch("app.models.user.chat_json", side_effect=fake_chat_json), \
                patch.object(profile, "_save"):
            profile.analyze([{"role": "user", "content": "今天很好"}])
        self.assertNotIn("用户住在杭州", captured[0])
        self.assertIn("用户住在成都", captured[0])

    def test_valid_correction_cannot_leave_model_returned_old_summary(self):
        profile = UserProfile(None, path=str(TEST_TMP_ROOT / f"profile-{uuid.uuid4().hex}.json"))
        profile.data.update({
            "summary": "用户住在杭州",
            "facts": ["用户住在杭州", "用户养了一只狗"],
        })
        response = {
            "summary": "用户住在杭州",
            "facts": ["用户住在成都"],
            "corrections": [{
                "field": "facts", "old": "用户住在杭州", "new": "用户住在成都",
                "quote": "我搬到成都了",
            }],
        }
        with patch("app.models.user.chat_json", return_value=(response, "")), \
                patch.object(profile, "_save"):
            profile.analyze([{"role": "user", "content": "我搬到成都了"}])
        self.assertNotIn("杭州", profile.render())
        self.assertIn("用户住在成都", profile.render())

    def test_invalid_model_profile_types_are_ignored_and_renderable(self):
        profile = UserProfile(None, path=str(TEST_TMP_ROOT / f"profile-{uuid.uuid4().hex}.json"))
        response = {
            "facts": ["用户住在成都", {"bad": "事实"}, 7, ""],
            "preferences": ["用户喜欢短答", ["bad"]],
            "topics": [{"bad": "主题"}, "Python"],
            "expertise": {"Python": "熟练", "坏值": {"nested": True}, 3: "数字键"},
            "metadata": {"internal": True},
        }
        with patch("app.models.user.chat_json", return_value=(response, "")), \
                patch.object(profile, "_save"):
            profile.analyze([{"role": "user", "content": "我住在成都，喜欢短答，最近学 Python"}])
        self.assertEqual(profile.data["facts"], ["用户住在成都"])
        self.assertEqual(profile.data["preferences"], ["用户喜欢短答"])
        self.assertEqual(profile.data["topics"], ["Python"])
        self.assertEqual(profile.data["expertise"], {"Python": "熟练"})
        self.assertNotIn("metadata", profile.data)
        rendered = profile.render()
        self.assertIn("用户住在成都", rendered)
        self.assertNotIn("bad", rendered)

    def test_non_list_history_is_replaced_before_valid_correction(self):
        profile = UserProfile(None, path=str(TEST_TMP_ROOT / f"profile-{uuid.uuid4().hex}.json"))
        profile.data.update({"facts": ["用户住在杭州"], "history": {"bad": "shape"}})
        response = {"corrections": [{
            "field": "facts", "old": "用户住在杭州", "new": "用户住在成都",
            "quote": "我搬到成都了",
        }]}
        with patch("app.models.user.chat_json", return_value=(response, "")), \
                patch.object(profile, "_save"):
            profile.analyze([{"role": "user", "content": "我搬到成都了"}])
        self.assertIsInstance(profile.data["history"], list)
        self.assertEqual(len(profile.data["history"]), 1)
        self.assertEqual(profile.data["history"][0]["new"], "用户住在成都")

    def test_invalid_existing_field_shape_does_not_break_correction_validation(self):
        profile = UserProfile(None, path=str(TEST_TMP_ROOT / f"profile-{uuid.uuid4().hex}.json"))
        profile.data.update({"facts": None})
        response = {"corrections": [{
            "field": "facts", "old": "用户住在杭州", "new": "用户住在成都",
            "quote": "我搬到成都了",
        }]}
        with patch("app.models.user.chat_json", return_value=(response, "")), \
                patch.object(profile, "_save"):
            profile.analyze([{"role": "user", "content": "我搬到成都了"}])
        self.assertEqual(profile.data.get("history", []), [])
        self.assertEqual(profile.render(), "")

    def test_correction_removes_exact_old_copies_from_other_profile_lists(self):
        profile = UserProfile(None, path=str(TEST_TMP_ROOT / f"profile-{uuid.uuid4().hex}.json"))
        old = "用户喜欢简短回答"
        profile.data.update({
            "facts": [old, "用户住在成都"],
            "preferences": [old, "用户喜欢轻松交流"],
            "topics": [old, "Python"],
        })
        response = {"corrections": [{
            "field": field, "old": old, "new": "用户希望详细说明",
            "quote": "以后详细说",
        } for field in ("preferences", "facts", "topics")]}
        with patch("app.models.user.chat_json", return_value=(response, "")), \
                patch.object(profile, "_save"):
            profile.analyze([{"role": "user", "content": "以后详细说"}])
        for field in ("facts", "preferences", "topics"):
            with self.subTest(field=field):
                self.assertNotIn(old, profile.data[field])
        self.assertIn("用户住在成都", profile.data["facts"])
        self.assertIn("用户喜欢轻松交流", profile.data["preferences"])
        self.assertIn("Python", profile.data["topics"])
        self.assertEqual(profile.data["preferences"], ["用户喜欢轻松交流", "用户希望详细说明"])
        self.assertEqual(profile.data["facts"], ["用户住在成都"])
        self.assertEqual(profile.data["topics"], ["Python"])
        self.assertEqual(len(profile.data["history"]), 1)
        self.assertNotIn("简短", profile.render())


if __name__ == "__main__":
    unittest.main()

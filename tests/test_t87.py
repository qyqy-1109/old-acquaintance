"""T87: preference grounding and observable yielding in everyday choices."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import config
from app.services.chat_service import ChatService
from app.utils import agency


class AgencyTests(unittest.TestCase):
    def test_persona_cases_match_current_character(self):
        cases_path = ROOT / "benchmark" / "persona_cases.json"
        payload = json.loads(cases_path.read_text(encoding="utf-8"))
        cases = {case["id"]: case for case in payload["cases"]}

        def text(case_id):
            return json.dumps(cases[case_id], ensure_ascii=False)

        self.assertNotIn("领导", text("PERS01"))
        self.assertNotIn("房租", text("PERS02"))
        self.assertNotIn("同事", text("PERS05"))
        self.assertIn("答应的事", text("PERS04"))
        self.assertIn("球场", text("PERS04"))
        self.assertIn("打球", text("PERS07"))
        self.assertIn("追剧", text("PERS07"))
        self.assertNotIn("上班", text("PERS07"))
        self.assertNotIn("下班", text("PERS07"))
        self.assertIn("团子", text("PERS10"))
        self.assertIn("银渐层", text("PERS10"))
        self.assertNotIn("年糕", text("PERS10"))
        self.assertIn("国内老家", text("PERS11"))
        self.assertIn("隔一两周", text("PERS11"))
        self.assertNotIn("视频", text("PERS11"))
        self.assertNotIn("阳台上的花", text("PERS11"))
        self.assertIn("学校球场", text("PERS12"))
        self.assertIn("打球", text("PERS12"))
        self.assertNotIn("菜市场", text("PERS12"))
        self.assertNotIn("咖啡馆", text("PERS12"))
        legacy_quote_fragments = {
            "PERS01": "热心，先帮你想办法",
            "PERS02": "有判断力，觉得你不对",
            "PERS03": "轻松、带点俏皮",
            "PERS04": "效率优先于形式",
            "PERS05": "先给结论，再解释为什么",
            "PERS06": "拿不准先问清楚",
            "PERS08": "列点用短横线",
        }
        for case_id, fragment in legacy_quote_fragments.items():
            with self.subTest(case_id=case_id):
                self.assertNotIn(fragment, text(case_id))

    def test_only_questions_about_her_choice_are_observed(self):
        for question in ("周末球场还是后街小吃摊？你来选", "你更想去哪里？", "今天听你的，去哪？"):
            with self.subTest(question=question):
                self.assertTrue(agency.asks_for_her_choice(question))
        for question in ("你好吗？", "你觉得我该辞职吗？", "明天周一周二都能收快递吗？"):
            with self.subTest(question=question):
                self.assertFalse(agency.asks_for_her_choice(question))

    def test_choice_hint_names_existing_sources_only(self):
        events = [
            {"text": "周末选了去学校球场", "source": "model"},
            {"text": "晚上：在家追剧", "source": "fallback"},
        ]
        hint = agency.preference_hint(
            "周末去哪？你来选", ["喜欢去学校里的球场"], events)
        self.assertIn("喜欢去学校里的球场", hint)
        self.assertIn("周末选了去学校球场", hint)
        self.assertNotIn("在家追剧", hint)
        self.assertIn("依据", hint)
        self.assertIn("不证明具体往事", hint)
        self.assertNotIn("先说你的倾向", hint)
        self.assertNotIn("不收回自己的倾向", hint)
        self.assertIn("对方自己的重大决定仍由他拍板", hint)
        self.assertEqual(agency.preference_hint("你好吗？", ["喜欢去学校里的球场"], events), "")

    def test_yield_cues_are_candidates_not_answer_rewrites(self):
        self.assertEqual(
            agency.yield_cues("周末球场还是后街小吃摊？你来选", "我不挑，都行，随你"),
            ["我不挑", "都行", "随你"],
        )
        self.assertEqual(
            agency.yield_cues("明天能收快递吗？", "周一周二都行"), [],
        )
        self.assertEqual(
            agency.yield_cues("周末球场还是后街小吃摊？你来选", "我更想去球场，想投两球"), [],
        )
        self.assertEqual(
            agency.yield_cues("周末去哪？你来选", "我想去球场，可你想去后街小吃摊就当我没说"),
            ["就当我没说"],
        )

    def test_chat_prompt_grounds_her_choice_without_changing_other_questions(self):
        service = ChatService.__new__(ChatService)
        service.life = SimpleNamespace(
            render=lambda: "",
            recent_events=lambda limit: [{"text": "她自己选了去学校球场", "source": "model"}],
        )
        service.weather = SimpleNamespace(enabled=False)
        service.store = SimpleNamespace(slots_all=lambda **kwargs: [])
        service.personality = SimpleNamespace(
            render=lambda: "人设正文", data={"favorite_places": ["喜欢去学校里的球场"]})
        service.profile = SimpleNamespace(render=lambda: "")
        service._nearby_block = ""
        with patch.object(config, "INJECT_TIME", False), patch.object(config, "AUTO_EMOTION", False):
            choice_prompt = service.system_prompt("球场还是后街小吃摊？你来选")
            ordinary_prompt = service.system_prompt("你好吗？")
        self.assertIn("喜欢去学校里的球场", choice_prompt)
        self.assertIn("她自己选了去学校球场", choice_prompt)
        self.assertNotIn("【你的选择依据】", ordinary_prompt)
        self.assertIn("人设正文", ordinary_prompt)

    def test_prompt_can_reach_past_fallback_life_events(self):
        service = ChatService.__new__(ChatService)
        history = ([{"text": "系统兜底", "source": "fallback"}] * 5
                   + [{"text": "自己选了去学校球场", "source": "model"}])
        service.life = SimpleNamespace(
            render=lambda: "", recent_events=lambda limit: history[:limit])
        service.weather = SimpleNamespace(enabled=False)
        service.store = SimpleNamespace(slots_all=lambda **kwargs: [])
        service.personality = SimpleNamespace(
            render=lambda: "人设正文", data={"favorite_places": ["喜欢去学校里的球场"]})
        service.profile = SimpleNamespace(render=lambda: "")
        service._nearby_block = ""
        with patch.object(config, "INJECT_TIME", False), patch.object(config, "AUTO_EMOTION", False):
            prompt = service.system_prompt("周末去哪？你来选")
        self.assertIn("自己选了去学校球场", prompt)

    def test_prepare_uses_this_questions_choice_prompt(self):
        service = ChatService.__new__(ChatService)
        seen = {}
        service._advance_life = lambda: None
        service._observe_user_life = lambda question: None
        service.speaker = SimpleNamespace(touch_user=lambda: None)
        service._nearby_for = lambda question: ""
        service.system_prompt = lambda question: f"针对：{question}"

        def build_messages(prompt, question):
            seen["prompt"] = prompt
            return [{"role": "system", "content": prompt}], []

        service.store = SimpleNamespace(build_messages=build_messages)
        service.life = SimpleNamespace(history_for=lambda question, limit: "")
        service.prepare("球场还是后街小吃摊？你来选")
        self.assertEqual(seen["prompt"], "针对：球场还是后街小吃摊？你来选")

    def test_finish_turn_records_yield_cue_and_preserves_answer(self):
        saved = []
        events = []
        service = ChatService.__new__(ChatService)
        service.store = SimpleNamespace(
            add_turn=lambda question, answer: saved.append((question, answer)) or "",
            save=lambda: None,
            event=lambda kind, **fields: events.append({"kind": kind, **fields}),
            session="test-session", sessions={"test-session": []},
        )
        service.observe_life_reply = lambda answer: None
        service.weather = SimpleNamespace(enabled=False)
        service._pending_context = None
        service._turns_since_profile = 0
        answer = "我不挑，都行，随你。"
        with patch.object(config, "AUTO_EMOTION", False), \
             patch.object(config, "AUTO_EVOLVE_PERSONALITY", False), \
             patch.object(config, "PROFILE_EVERY", 999):
             service.finish_turn("球场还是后街小吃摊？你来选", answer)
        self.assertEqual(saved, [("球场还是后街小吃摊？你来选", answer)])
        self.assertEqual(service.last_turn["answer"], answer)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "agency_yield_candidate")
        self.assertEqual(events[0]["cues"], ["我不挑", "都行", "随你"])

    def test_persona_runner_passes_choice_question_to_prompt(self):
        program = """
from benchmark import run_persona
class Service:
    def __init__(self):
        self.questions = []
    def system_prompt(self, question):
        self.questions.append(question)
        return 'system'
    def ask(self, messages):
        return '球场'
service = Service()
answers = run_persona._ask(service, [{'u': '球场还是后街小吃摊？你来选'}])
assert answers == ['球场']
assert service.questions == ['球场还是后街小吃摊？你来选']
"""
        result = subprocess.run(
            [sys.executable, "-c", program], cwd=ROOT,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_persona_off_baseline_has_no_favorite_places(self):
        program = """
from benchmark import run_persona
from app.models.personality import Personality
data = Personality._migrate(dict(run_persona.PERSONA_OFF))
assert data['favorite_places'] == [], data['favorite_places']
"""
        result = subprocess.run(
            [sys.executable, "-c", program], cwd=ROOT,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_persona_cases_do_not_inherit_prior_life_choices(self):
        program = """
import sqlite3
from types import SimpleNamespace
from benchmark import run_persona
db = sqlite3.connect(':memory:')
for table in ('memories', 'messages', 'summaries', 'pending', 'slots',
              'life_state', 'life_events'):
    db.execute(f'CREATE TABLE {table} (id INTEGER)')
    db.execute(f'INSERT INTO {table} VALUES (1)')
store = SimpleNamespace(db=db)
service = SimpleNamespace(store=store, world=SimpleNamespace(entries=[]),
    emotion=SimpleNamespace(entries=[]), profile=SimpleNamespace(data={}))
run_persona._clear(service)
for table in ('life_state', 'life_events'):
    assert db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0, table
"""
        result = subprocess.run(
            [sys.executable, "-c", program], cwd=ROOT,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_persona_life_probe_seeds_only_her_group(self):
        program = """
from types import SimpleNamespace
from benchmark import run_persona
events = []
seen = []
run_persona._clear = lambda service: events.clear()
run_persona._ask = lambda service, turns: seen.append(list(events)) or ['我想去学校球场']
run_persona._classify = lambda service, case, answer: {'class': 'A', 'ok': True}
life = SimpleNamespace(_add_event=lambda slot, text, source, at: events.append((text, source)))
service = SimpleNamespace(life=life)
case = {'id': 'PERS13', 'dim': 'agency', 'probe': '今晚你选',
        'life_event': '最近放学后自己去了学校球场，投了几球，最后赢了一局，挺满足', 'rules': {}}
on = run_persona._run_group(service, [case], 'on', '她', 1)
run_persona._run_group(service, [case], 'off', '助手', 1)
assert seen == [[('最近放学后自己去了学校球场，投了几球，最后赢了一局，挺满足', 'model')], []], seen
assert on[0]['life_event'] == case['life_event']
"""
        result = subprocess.run(
            [sys.executable, "-c", program], cwd=ROOT,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_persona_classifier_sees_the_exact_life_evidence(self):
        program = """
from benchmark import run_persona
seen = []
run_persona._ask_json = lambda service, prompt: (seen.append(prompt) or
    {'class': 'A', 'reason': '测试'}, '')
case = {'id': 'PERS13', 'dim': 'agency', 'probe': '今晚你选哪种安排？',
        'quote': '她有自己的日常选择', 'life_event': '最近放学后自己去了学校球场，投了几球，最后赢了一局，挺满足',
        'evidence_limit': '只知道她最近去了学校球场并觉得满足，不知道具体球技和比分',
        'class_a_required': 'A级须明确引用自己的经历',
        'classes': {'A': '有出处', 'D': '编经历'}, 'ok_class': 'A'}
run_persona._classify(None, case, '我想去学校球场')
assert case['life_event'] in seen[0], seen[0]
assert case['evidence_limit'] in seen[0], seen[0]
assert case['class_a_required'] in seen[0], seen[0]
"""
        result = subprocess.run(
            [sys.executable, "-c", program], cwd=ROOT,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_persona_consistency_compares_within_each_repeat_only(self):
        program = """
from benchmark import run_persona
seen = []
def check(service, items):
    seen.append(items)
    return {'conflicts': [], 'dropped': [], 'reason': '无冲突'}
run_persona._consistency = check
runs = [
    {'id': 'PERS12', 'round': 1, 'probe': '地点', 'answer': '学校球场'},
    {'id': 'PERS12', 'round': 2, 'probe': '地点', 'answer': '后街小吃摊'},
    {'id': 'PERS13', 'round': 1, 'probe': '安排', 'answer': '学校球场'},
    {'id': 'PERS13', 'round': 2, 'probe': '安排', 'answer': '出租屋追剧'},
]
result = run_persona._consistency_by_round(None, runs)
assert seen == [[('地点', '学校球场'), ('安排', '学校球场')],
                [('地点', '后街小吃摊'), ('安排', '出租屋追剧')]], seen
assert [item['round'] for item in result['rounds']] == [1, 2]
"""
        result = subprocess.run(
            [sys.executable, "-c", program], cwd=ROOT,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

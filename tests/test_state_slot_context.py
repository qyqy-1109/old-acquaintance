import datetime as dt
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import config
from app.models.memory import MemoryStore
from app.services.chat_service import ChatService


class StateSlotContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.store = MemoryStore(SimpleNamespace(), path=str(self.directory / 'memory.db'),
                                 embed_model='offline-test')
        self.addCleanup(self.store.db.close)
        self.service = object.__new__(ChatService)
        self.service.store = self.store
        self.service.user_id = ''
        self.service.personality = SimpleNamespace(
            display_name=lambda: '沈知夏', render=lambda: '沈知夏', data={})
        self.service.life = None
        self.service.continuity = SimpleNamespace(render=lambda *args, **kwargs: '')
        self.service.weather = SimpleNamespace(enabled=False)
        self.service.profile = SimpleNamespace(render=lambda: '')
        self.service._nearby_block = ''
        self.confirmed_at = dt.datetime(2026, 10, 17, 8, 30).timestamp()
        with patch('time.time', return_value=self.confirmed_at):
            self.store.upsert_slots([
                {'entity': '沈知夏', 'attr': '位置', 'value': '在车上，正前往宠物医院'},
                {'entity': '沈知夏', 'attr': '状态', 'value': '带猫去看医生'},
            ])
        with patch('time.time', return_value=self.confirmed_at + 7200):
            self.store.upsert_slots([
                {'entity': '团子', 'attr': '状态', 'value': '病后待观察'},
                {'entity': '沈知夏', 'attr': '状态', 'value': '刚忙完手头的事'},
            ])

    def test_state_command_marks_old_location_and_preserves_saved_value(self):
        original = self.store.slots_all()
        output = self.service.run_command('/slots', user_name='小林')
        self.assertIn('最后确认 2026-10-17 08:30', output)
        self.assertIn('这是当时的位置，之后未确认', output)
        self.assertIn('团子·状态：病后待观察', output)
        self.assertEqual(self.store.slots_all(), original)

    def test_chat_context_includes_time_for_relevant_old_location(self):
        with patch.object(config, 'AUTO_EMOTION', False):
            prompt = self.service.system_prompt('沈知夏现在在哪里')
        self.assertIn('最后确认 2026-10-17 08:30', prompt)
        self.assertIn('这是当时的位置，之后未确认', prompt)
        self.assertNotIn('团子·状态', prompt)

    def test_chat_context_keeps_her_old_location_beyond_recent_slot_limit(self):
        with patch('time.time', return_value=self.confirmed_at + 10800):
            self.store.upsert_slots([
                {'entity': f'邻居{index}', 'attr': '状态', 'value': '待确认'}
                for index in range(13)
            ])
        for question in ('沈知夏现在在哪里', '你现在在哪里'):
            with self.subTest(question=question), patch.object(config, 'AUTO_EMOTION', False):
                prompt = self.service.system_prompt(question)
                self.assertIn('沈知夏·位置：在车上，正前往宠物医院', prompt)
                self.assertIn('最后确认 2026-10-17 08:30', prompt)
                self.assertNotIn('邻居0·状态', prompt)


if __name__ == '__main__':
    unittest.main()

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from app import config
from app.services.chat_service import ChatService


class ProfileTurnSelectionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        for name, value in {
            'DATA_DIR': str(self.directory), 'API_KEY': 'test-key',
            'CHAT_API_KEY': 'test-key', 'EMBED_API_KEY': 'test-key',
            'AMAP_KEY': '', 'AMAP_CITY': '', 'CHECK_ON_START': False,
            'AUTO_EMOTION': False, 'AUTO_EVOLVE_PERSONALITY': False,
            'PROFILE_EVERY': 3,
            'MEMORY_CFG': dict(config.MEMORY_CFG, auto_extract=True, extract_every=1000,
                               summarize_every=0),
        }.items():
            self.stack.enter_context(patch.object(config, name, value))
        for target in ('httpx.Client.send', 'requests.sessions.Session.request',
                       'urllib.request.urlopen'):
            self.stack.enter_context(patch(target, side_effect=AssertionError('测试禁止联网')))
        self.service = ChatService()
        self.addCleanup(self.service.close)

    def test_proactive_messages_do_not_displace_recent_user_evidence(self):
        history = self.service.store.sessions.setdefault(self.service.store.session, [])
        history.extend([
            {'role': 'user', 'content': '我叫小林'},
            {'role': 'assistant', 'content': '我叫沈知夏'},
            {'role': 'user', 'content': '我喜欢骑车'},
        ])
        history.extend({'role': 'assistant', 'content': f'她的主动消息{index}',
                        'proactive': True} for index in range(10))
        self.service._turns_since_profile = 2
        with patch.object(self.service.profile, 'analyze', return_value='') as analyze:
            self.service.finish_turn('我已经搬到成都', '记住啦')
        selected = analyze.call_args.args[0]
        self.assertEqual([message['content'] for message in selected],
                         ['我叫小林', '我喜欢骑车', '我已经搬到成都'])
        self.assertTrue(all(message['role'] == 'user' for message in selected))


if __name__ == '__main__':
    unittest.main()

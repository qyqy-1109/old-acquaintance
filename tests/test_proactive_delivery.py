# -*- coding: utf-8 -*-
"""主动说话的**投递口**兜底（`#146` 的同一条纪律，补在投递这一侧）。

生成口（`Speaker.speak()`）早就有一道同类护栏：她会拿**只有她自己知道**的东西
（团子／球衣／课表／出租屋…见 `Relationship.her_own()`）反过来问他时，
那一条会被丢掉。**但投递口没有**——真机库里就躺着一条 2026-10-01 生成的
「团子没扒窗户吧」，只要服务起来就会发出去，而他答不上来。

所以这份契约测的是投递口：`Speaker.drop_own_questions(items)` ——
· 收 `pending_items()` 那样的列表，返回**仍该投递的**（保持原顺序）；
· 丢掉的原文进 `speaker.last_delivery_drops`；
· 丢掉的行**同时标记已投递**（之后再 `pending_items()` 不再返回它们）；
· `take_pending()` 也必须过这道兜底，不能绕过；
· 没挂关系层时**原样返回**、不抛异常；
· 正常报备（「在收球衣，有件掉地上了，刚下楼捡回来」）**不许误杀**。

纪律：不联网、不起服务、不跑真实模型；用临时 `DATA_DIR` 隔离，
**绝不碰根目录真实的 `memory.db`**。

跑法：`python -m unittest tests.test_proactive_delivery -q`
"""

import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config                                                       # noqa: E402
from app.services.chat_service import ChatService                             # noqa: E402

OWN_QUESTION = "团子没扒窗户吧"                     # 真机现场那条：她在问自己的猫
REPORT = "在收球衣，有件掉地上了，刚下楼捡回来"        # 同一次开口的报备条：必须照常投递
OTHER = "刚听到首挺好听的歌"                          # 无关的一条：不能被误杀


class ProactiveDeliveryTests(unittest.TestCase):
    """投递口兜底：她自己的东西不许反过来问他。"""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # 临时 DATA_DIR：所有落盘（memory.db / relationship.json …）都在这里面，
        # 进程结束即删——**真实 memory.db 一行都不会被碰**。
        self.directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        settings = {
            "DATA_DIR": self.directory,
            "API_KEY": "test-key", "CHAT_API_KEY": "test-key", "EMBED_API_KEY": "test-key",
            "AMAP_KEY": "", "AMAP_CITY": "",
        }
        for name, value in settings.items():
            self.stack.enter_context(patch.object(config, name, value))
        self.service = ChatService()
        self.addCleanup(self.service.close)
        self.speaker = self.service.speaker
        self.relationship = self.service.relationship

    # ---------- 工具 ----------
    def queue(self, *texts):
        """把她说过的话排进待发表，返回 `pending_items()` 那样的快照。"""
        for text in texts:
            self.speaker.enqueue(text)
        return self.speaker.pending_items()

    def texts(self, items):
        return [row["text"] for row in items]

    def pending_texts(self):
        return self.texts(self.speaker.pending_items())

    # ---------- 契约 1：丢掉命中的、留下其余的、保持原顺序 ----------
    def test_own_question_is_dropped_and_order_is_kept(self):
        self.assertTrue(callable(getattr(self.speaker, "drop_own_questions", None)),
                        "投递口兜底必须是 Speaker.drop_own_questions(items)")
        items = self.queue(REPORT, OWN_QUESTION, OTHER)
        kept = self.speaker.drop_own_questions(items)
        self.assertEqual(self.texts(kept), [REPORT, OTHER])
        # 返回的必须还是 `pending_items()` 那样的行——投递方要拿 id 去 ack
        self.assertEqual([row["id"] for row in kept], [items[0]["id"], items[2]["id"]])

    # ---------- 契约 2（`B2-②` 改）：挡下的行**不标记已读**，留在队列里可恢复/可审计 ----------
    def test_dropped_rows_stay_recoverable(self):
        items = self.queue(REPORT, OWN_QUESTION)
        kept = self.speaker.drop_own_questions(items)
        self.assertEqual(self.texts(kept), [REPORT], "只投递没被挡下的那条")
        self.assertIn(OWN_QUESTION, list(self.speaker.last_delivery_drops), "要留痕")
        self.assertTrue(list(self.speaker.last_delivery_drop_ids), "还要留下行 id（可人工放行）")
        # 关键：它**没有**被标成已读——所以看板看得见、能人工恢复（原来命中即 ack＝不可恢复）
        self.assertIn(OWN_QUESTION, self.pending_texts())
        self.assertEqual(len(self.pending_texts()), 2,
                         "两条都还在队列里：一条待投递、一条被挡下但可恢复")

    # ---------- 契约 3：丢掉的原文留证 ----------
    def test_dropped_text_is_recorded(self):
        items = self.queue(OWN_QUESTION, REPORT)
        self.speaker.drop_own_questions(items)
        self.assertIn(OWN_QUESTION, list(self.speaker.last_delivery_drops))
        self.assertNotIn(REPORT, list(self.speaker.last_delivery_drops))

    # ---------- 契约 4：`take_pending()` 不能绕过 ----------
    def test_take_pending_cannot_bypass_the_gate(self):
        self.queue(REPORT, OWN_QUESTION)
        delivered = self.speaker.take_pending()
        self.assertEqual(self.texts(delivered), [REPORT])
        self.assertIn(OWN_QUESTION, list(self.speaker.last_delivery_drops))
        # 幸存者被 ack 了；被挡下的那条**仍然留着**（`B2-②`：可恢复、可审计）
        self.assertEqual(self.pending_texts(), [OWN_QUESTION])

    # ---------- 契约 5：没挂关系层 → 原样返回，不抛异常 ----------
    def test_without_relationship_layer_items_pass_through(self):
        items = self.queue(OWN_QUESTION, REPORT)
        own = self.relationship.her_own()          # 默认清单里确实带着「团子」
        self.assertIn("团子", own)
        self.speaker.relationship = None
        kept = self.speaker.drop_own_questions(items)
        self.assertEqual(self.texts(kept), self.texts(items))
        self.assertEqual(self.pending_texts(), self.texts(items))

    # ---------- 契约 6：正常报备不许误杀 ----------
    def test_plain_report_is_not_killed(self):
        items = self.queue(REPORT)
        kept = self.speaker.drop_own_questions(items)
        self.assertEqual(self.texts(kept), [REPORT])
        self.assertEqual(self.pending_texts(), [REPORT])
        self.assertEqual(list(self.speaker.last_delivery_drops), [])


if __name__ == "__main__":
    unittest.main()

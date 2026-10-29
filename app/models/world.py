"""世界书：关于「你和用户共同生活的这个世界」的长期设定。

先说它和其它三层有什么区别：

    personality   我是谁
    world         这个世界长什么样
    user          用户是个什么样的人
    memory        我们具体聊过什么

值得单开一层，是因为它跟记忆的生命周期不一样。
比如「用户养了只叫年糕的猫」——如果把它塞进普通记忆，三个月不提一句，
衰减机制就把它淡忘了，可它作为背景设定其实一直成立；
反过来也不该塞进性格模板，那是用户的事，不是「我」的自我认知。

所以它自己一层：不受遗忘机制淘汰，但也不写在人设里污染她的自我认知。

为什么不做关键词触发：
    传统世界书（SillyTavern 那套）靠关键词匹配 + 优先级 + 插入位置来投放条目，
    那是为了在有限的上下文里精确控制每一条的出现时机。
    这里条目撑死几十上百条，直接用已有的向量召回按语义投放就够了，
    再写一套关键词引擎属于重复建设。

数据存在 world.json —— 这是唯一真相，可以直接用记事本手写。
启动时同步一次到向量库；对话里提炼出来的背景小节也会回调写回来。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from app.utils.llmjson import chat_json, strings

from app.log import get

log = get("world")

HINT = (
    "每一行就是一个设定。可以写你们住在哪、家里养了什么、常去的地方、"
    "长期在做的事、你们之间反复出现的话题。写得越具体越好，"
    "比如不要写「她养了宠物」，写「她养了一只银渐层猫，叫团子，爱趴在阳台」。"
)

TIDY_PROMPT = """下面是一份「世界书」设定清单，里面存在重复、笼统冗余、可以合并的条目。

{entries}

请整理成一份干净的清单：

- 说的是同一件事的，合并成一条，保留信息量最大的那种写法。
- 特别笼统、已经被更具体的条目覆盖的，直接删掉。
- 各自独立的信息保持独立，不要为了篇幅把它们硬揉在一起。
- 不要新增原文里没有的任何信息，也不要修改事实。
- 每条一句话，主语写清楚，脱离上下文也能看懂。

只输出 JSON，不要解释：{{"entries": ["...", "..."]}}
"""

EMPTY: dict[str, Any] = {"hint": HINT, "entries": []}


class WorldBook:
    def __init__(self, store, path: str = "world.json"):
        self.store = store
        self.path = path
        self.entries: list[dict] = []  # [{"text","ts","source"}]
        self._load()
        # 对话里提炼出的背景条目，经这个回调落盘，保证 world.json 始终是唯一真相
        self.store.on_world = self._auto_add
        self.store.on_drop = self._auto_drop
        self._absorb_store()

    def _auto_drop(self, texts: list[str]) -> None:
        """对话里判定某条记忆已作废时，如果它同时也是世界书条目，就从 json 里删掉。

        被作废的常常只是普通事实（不在世界书里），那就什么都不用做——
        世界书里没有对应条目时这里自然是个空操作。
        """
        want = set(texts)
        before = len(self.entries)
        self.entries = [e for e in self.entries if e["text"] not in want]
        if len(self.entries) != before:
            self._save()

    def _absorb_store(self) -> None:
        """向量库里已经有世界书条目时（比如先聊过才建 WorldBook），补进 json。

        不这么做的话，sync() 会把它们当成"文件里已经删掉"给清出去。
        """
        have = {e["text"] for e in self.entries}
        got = [t for t in self.store.world_texts() if t not in have]
        for text in got:
            self.entries.append({"text": text, "source": "对话提炼", "ts": time.time()})
        if got:
            self._save()
            log.info("已从记忆里回收 %d 条背景设定", len(got))

    # ---------- 持久化 ----------
    def _load(self) -> None:
        if not os.path.exists(self.path):
            self._save()  # 首次运行，生成一个带说明的空模板
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            log.error("读取失败，按空处理：%s", exc)
            return
        raw = data.get("entries", []) if isinstance(data, dict) else data
        for item in raw or []:
            entry = self._normalize(item)
            if entry:
                self.entries.append(entry)

    @staticmethod
    def _normalize(item: Any) -> dict | None:
        """兼容两种写法：直接写字符串，或者写成对象带备注。"""
        if isinstance(item, str):
            text = item.strip()
            source = "manual"
        elif isinstance(item, dict):
            text = str(item.get("text", "")).strip()
            source = str(item.get("source", "manual"))
        else:
            return None
        if not text:
            return None
        return {"text": text, "source": source, "ts": time.time()}

    def _save(self) -> None:
        # 这段原本就是正确的写法；抽成公共工具后让所有 JSON 走同一条路
        # （见 app/utils/jsonfile.py），省得以后又漏掉某一处。
        from app.utils.jsonfile import write_json_atomic
        try:
            write_json_atomic(self.path, {"hint": HINT, "entries": self.entries})
        except Exception as exc:
            log.error("保存失败：%s: %s", type(exc).__name__, exc)

    # ---------- 同步 ----------
    def sync(self) -> str:
        """把 json 里的条目灌进向量库，同时清掉已经从文件里删掉的那部分。"""
        added, dropped = self.store.sync_world([e["text"] for e in self.entries])
        if not added and not dropped:
            return ""
        return f"世界书已同步：新增 {added} 条，移除 {dropped} 条"

    def _auto_add(self, texts: list[str]) -> None:
        for text in texts:
            if any(e["text"] == text for e in self.entries):
                continue
            self.entries.append({"text": text, "source": "对话提炼", "ts": time.time()})
        self._save()

    # ---------- 操作 ----------
    def add(self, text: str) -> str:
        text = text.strip()
        if not text:
            return "（没写内容）"
        if any(e["text"] == text for e in self.entries):
            return f"这条已经在世界书里了：{text}"
        self.entries.append({"text": text, "source": "manual", "ts": time.time()})
        self._save()
        self.store.add_world(text)
        return f"世界书已记下：{text}"

    def import_entries(self, texts: list[str], source: str = "人格卡片",
                       sync: bool = True) -> str:
        """Bulk import from a persona card; save once, sync once."""
        added = []
        for raw in texts or []:
            text = str(raw or "").strip()[:2000]
            if not text or any(e["text"] == text for e in self.entries):
                continue
            self.entries.append({"text": text, "source": source, "ts": time.time()})
            added.append(text)
            if len(added) >= 50:
                break
        if not added:
            return "没有新增世界书条目"
        self._save()
        if not sync:
            return f"已导入 {len(added)} 条世界书（等待配置 embedding 后同步）"
        try:
            self.sync()
        except Exception as exc:
            # 向量库同步失败不该丢掉刚刚写入的文件；下次启动会再同步。
            log.warning("导入世界书后同步失败（文件已保存）：%s: %s",
                        type(exc).__name__, exc)
        return f"已导入 {len(added)} 条世界书"

    def remove(self, index: int) -> str:
        if not (0 <= index < len(self.entries)):
            return f"没有第 {index + 1} 条"
        got = self.entries.pop(index)
        self._save()
        self.sync()
        return f"已从世界书移除：{got['text']}"

    def tidy(self) -> str:
        """把重复、笼统、可合并的条目整理成干净清单。

        刻意做成手动触发：这事要额外调一次模型，放在每轮对话里跑没必要，
        等条目攒乱了、或者像现在这样发现重复了，喊一声就行。
        """
        if len(self.entries) < 2:
            return "世界书条目太少，没什么可整理的"
        block = "\n".join(f"{i + 1}. {e['text']}" for i, e in enumerate(self.entries))
        data, err = chat_json(
            self.store.client, self.store.extract_model,
            [{"role": "user", "content": TIDY_PROMPT.format(entries=block)}],
            temperature=0.2, max_tokens=3000, retries=2,
            no_think=bool(self.store.cfg.get("no_think")),
        )
        if data is None:
            return f"整理失败：{err}"
        merged = strings(data, "entries", "world")
        if not merged:
            return "整理后一条都不剩，这次整理已放弃"
        before = len(self.entries)
        self.entries = [{"text": t, "source": "整理合并", "ts": time.time()} for t in merged]
        self._save()
        self.sync()
        return f"世界书已整理：{before} 条 → {len(self.entries)} 条\n" + self.render()

    # ---------- 查看 ----------
    def count(self) -> int:
        return len(self.entries)

    def render(self) -> str:
        if not self.entries:
            return "（世界书还是空的）\n" + HINT + "\n用法：/world 我们住成都，她养了只银渐层叫团子"
        lines = [f"世界书共 {len(self.entries)} 条："]
        for i, e in enumerate(self.entries, 1):
            tag = "" if e.get("source") == "manual" else f"（{e['source']}）"
            lines.append(f"  {i}. {e['text']}{tag}")
        lines.append("删除：/world del 序号　·　合并重复和冗余：/world tidy")
        return "\n".join(lines)

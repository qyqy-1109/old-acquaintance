"""Isolated before/current companionship evaluation harness.

The default mode only builds prompts in temporary data directories. ``--live``
is the explicit switch that permits model requests. Raw JSON is the audit
record; the markdown file intentionally contains no naturalness score.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import random
import re
import socket
import subprocess
import sys
import queue
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = Path(__file__).with_name("companionship_cases.json")
OUTPUT_DIR = ROOT / "_tmptest" / "natural-193-196"
SNAPSHOT_DIR = OUTPUT_DIR / "before"
WORKER_MARKER = "__COMPANIONSHIP_RESULT__"
SECRET_KEYS = ("api_key", "authorization", "access_token", "refresh_token",
               "secret", "password", "cookie")
FIXED_START = dt.datetime(2026, 10, 15, 16, 0)
DEFAULT_REQUEST_TIMEOUT = 60.0
DEFAULT_WORKER_TIMEOUT = 900.0
FIXTURE_LIFE_TEXT = "收好画具后坐下来休息，才发现袖口沾了一点蓝颜料"


def load_cases(path: str | Path | None = None) -> list[dict[str, Any]]:
    source = Path(path) if path else CASES_PATH
    with source.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list):
        raise ValueError("案例文件必须包含 cases 数组")
    validate_cases(cases)
    return cases


def validate_cases(cases: list[dict[str, Any]]) -> None:
    if len(cases) != 12:
        raise ValueError(f"需要 12 组案例，得到 {len(cases)}")
    ids = set()
    for case in cases:
        if not isinstance(case, dict) or not str(case.get("id", "")).strip():
            raise ValueError("案例必须有非空 id")
        case_id = str(case["id"])
        if case_id in ids:
            raise ValueError(f"案例 id 重复：{case_id}")
        ids.add(case_id)
        turns = case.get("turns")
        if not isinstance(turns, list) or len(turns) != 3:
            raise ValueError(f"{case_id} 必须有三轮输入")
        for turn in turns:
            if not isinstance(turn, dict) or not str(turn.get("input", "")).strip():
                raise ValueError(f"{case_id} 有空输入")
        forbidden = {"expected", "expected_output", "score", "correct_answer"}
        if forbidden.intersection(case):
            raise ValueError(f"{case_id} 不允许固定答案或自动评分字段")


def _redact(value: Any, key: str = "") -> Any:
    if any(fragment in key.lower() for fragment in SECRET_KEYS):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(field): _redact(item, str(field)) for field, item in value.items()
                if not any(fragment in str(field).lower() for fragment in SECRET_KEYS)}
    if isinstance(value, list):
        return [_redact(item, key) for item in value]
    return value


def sanitize_model_params(params: dict[str, Any]) -> dict[str, Any]:
    clean = _redact(copy.deepcopy(params))
    return clean if isinstance(clean, dict) else {}


def canonical_hash(payload: Any) -> str:
    encoded = json.dumps(_redact(payload), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_blind_order(entries: list[dict[str, Any]], seed: int = 193196) -> list[dict[str, str]]:
    indexed = list(enumerate(entries, start=1))
    rng = random.Random(seed)
    rng.shuffle(indexed)
    result = []
    for position, (index, _entry) in enumerate(indexed, start=1):
        item_id = "item-" + canonical_hash({"seed": seed, "index": index})[:16]
        result.append({"blind_id": f"blind-{position:04d}", "item_id": item_id})
    return result


def build_messages(system: str, history: list[dict[str, str]], question: str) -> list[dict[str, str]]:
    return [{"role": "system", "content": system}, *copy.deepcopy(history),
            {"role": "user", "content": question}]


def build_fixture_bundle(personality: dict, relationship: dict) -> dict:
    copied_personality = copy.deepcopy(personality)
    copied_relationship = copy.deepcopy(relationship)
    copied_relationship.update(stage="熟络", decide="she", since="2026-09-15")
    return {"template": "app.models.personality.DEFAULT_PERSONALITY",
            "personality": copied_personality, "relationship": copied_relationship,
            "personality_hash": canonical_hash(copied_personality),
            "relationship_hash": canonical_hash(copied_relationship)}


def recover_entries(cases: list[dict], completed: list[dict], error: dict,
                    active: dict | None = None) -> list[dict]:
    recorded = {(entry["id"], entry["round"]): entry for entry in completed}
    recovered = []
    for case in cases:
        history = copy.deepcopy(case.get("seed_history", []))
        for round_number, turn in enumerate(case["turns"], start=1):
            key = (case["id"], round_number)
            entry = recorded.get(key)
            if entry is None:
                current_active = active if active and (active.get("id"), active.get("round")) == key else {}
                entry = {"id": case["id"], "group": case["group"], "round": round_number,
                         "input": current_active.get("input") or [*copy.deepcopy(history),
                            {"role": "user", "content": turn["input"]}],
                         "input_status": "captured_request" if current_active.get("input") else "planned_history_only",
                         "raw_output": None, "raw_response": None, "product_output": None,
                         "model_params": current_active.get("model_params", {}),
                         "calls": current_active.get("calls", []), "error": copy.deepcopy(error),
                         "fixture_history": case.get("seed_history", [])}
                entry["hash"] = canonical_hash(entry)
            recovered.append(entry)
            history.append({"role": "user", "content": turn["input"]})
            output = entry.get("product_output") or entry.get("raw_output")
            if output:
                history.append({"role": "assistant", "content": "\n".join(output)
                                if isinstance(output, list) else str(output)})
    return recovered


class ModelRecorder:
    def __init__(self, client, live: bool, gate=None, request_timeout=DEFAULT_REQUEST_TIMEOUT,
                 on_change=None):
        self.client = (client.with_options(timeout=request_timeout, max_retries=0)
                       if live and hasattr(client, "with_options") else client)
        self.live = live
        self.gate = gate
        self.on_change = on_change
        self.calls: list[dict[str, Any]] = []
        raw = SimpleNamespace(create=self.create_raw)
        completions = SimpleNamespace(create=self.create, with_raw_response=raw)
        self.chat = SimpleNamespace(completions=completions)

    def create(self, **kwargs):
        return self.create_raw(**kwargs).parse()

    def create_raw(self, **kwargs):
        params = sanitize_model_params({key: value for key, value in kwargs.items() if key != "messages"})
        call = {"input": copy.deepcopy(kwargs.get("messages", [])), "model_params": params,
                "raw_output": None, "raw_response": None, "status_code": None, "error": None}
        self.calls.append(call)
        if self.on_change:
            self.on_change(self.calls)
        if not self.live:
            call["error"] = {"type": "offline", "message": "model request skipped by default"}
            return SimpleNamespace(text="", parse=lambda: SimpleNamespace(choices=[]))
        try:
            with self.gate.model_request() if self.gate else _noop():
                response = self.client.chat.completions.with_raw_response.create(**kwargs)
            parsed = response.parse()
            call["raw_response"] = response.text
            call["status_code"] = getattr(response, "status_code", None)
            if parsed.choices:
                call["raw_output"] = parsed.choices[0].message.content
                if not str(call["raw_output"] or "").strip():
                    call["error"] = {"type": "empty_content", "message": "model returned empty content"}
            else:
                call["error"] = {"type": "empty_choices", "message": "model returned no choices"}
            if self.on_change:
                self.on_change(self.calls)
            return response
        except Exception as exc:
            call["error"] = _error(exc)
            response = getattr(exc, "response", None)
            if response is not None:
                call["raw_response"] = getattr(response, "text", None)
                call["status_code"] = getattr(response, "status_code", None)
            if self.on_change:
                self.on_change(self.calls)
            raise


@contextmanager
def _noop():
    yield


class NetworkGate:
    def __init__(self):
        self.allowed = False
        self.denied: list[str] = []
        original_connect = socket.socket.connect
        original_connect_ex = socket.socket.connect_ex

        def connect(sock, address):
            if not self.allowed:
                self.denied.append("socket.connect")
                raise AssertionError("network outside explicit model request")
            return original_connect(sock, address)

        def connect_ex(sock, address):
            if not self.allowed:
                self.denied.append("socket.connect_ex")
                raise AssertionError("network outside explicit model request")
            return original_connect_ex(sock, address)

        socket.socket.connect = connect
        socket.socket.connect_ex = connect_ex
        import httpx
        original_send = httpx.Client.send

        def send(client, request, *args, **kwargs):
            if not self.allowed:
                self.denied.append("httpx.Client.send")
                raise AssertionError("HTTP outside explicit model request")
            return original_send(client, request, *args, **kwargs)

        httpx.Client.send = send

    @contextmanager
    def model_request(self):
        self.allowed = True
        try:
            yield
        finally:
            self.allowed = False


def _code_hash(snapshot: Path) -> str:
    manifest = {str(path.relative_to(snapshot)).replace("\\", "/"):
                hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted((snapshot / "app").rglob("*.py"))}
    return canonical_hash(manifest)


def _fixture_files(data_dir: Path, personality: dict, relationship: dict) -> dict:
    bundle = build_fixture_bundle(personality, relationship)
    (data_dir / "personality.json").write_text(
        json.dumps({"current": bundle["personality"], "history": []}, ensure_ascii=False), encoding="utf-8")
    (data_dir / "relationship.json").write_text(
        json.dumps(bundle["relationship"], ensure_ascii=False), encoding="utf-8")
    for name, value in (("user_profile.json", {}), ("world.json", {"entries": []}),
                        ("emotion.json", [])):
        (data_dir / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return bundle


def seed_fixture_life_event(life, *, when: dt.datetime, text: str,
                            source: str = "model") -> dict:
    from app.models.life import slot_of

    life._add_event(slot_of(when), text, source, at=when.timestamp())
    row = life.recent_events(limit=1)[0]
    if row["text"] != text or row["source"] != source or row["ts"] != when.timestamp():
        raise RuntimeError("isolated life fixture was not persisted intact")
    return row


def _worker(snapshot: Path, case_path: Path, live: bool, request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
            checkpoint: Path | None = None) -> int:
    worker_dir = OUTPUT_DIR / ".worker-data" / f"{snapshot.name}-{uuid.uuid4().hex}"
    worker_dir.mkdir(parents=True, exist_ok=True)
    os.environ["DATA_DIR"] = str(worker_dir)
    os.environ["WB_AUTO_EMOTION"] = "0"
    os.environ["WB_AMAP_KEY"] = ""
    os.environ["PYTHONUTF8"] = "1"
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    gate = NetworkGate()
    try:
        sys.path.insert(0, str(snapshot.resolve()))
        from app.utils.envfile import load
        load(str(ROOT / ".env"))
        if not live:
            os.environ["WB_API_KEY"] = "offline-test-key"
            os.environ["WB_CHAT_API_KEY"] = "offline-test-key"
            os.environ["WB_EMBED_API_KEY"] = "offline-test-key"
        from unittest.mock import patch
        from app.services.chat_service import ChatService
        from app import config
        from app.models.life import slot_of
        from app.models.personality import DEFAULT_PERSONALITY
        from app.models.relationship import DEFAULT_RELATIONSHIP
        from app.utils import clock
        config.AUTO_EVOLVE_PERSONALITY = False
        config.AUTO_EMOTION = False
        config.AMAP_KEY = ""
        config.AMAP_CITY = ""
        config.CHECK_ON_START = False
        if not config.CHAT_API_KEY:
            config.CHAT_API_KEY = config.API_KEY
        cases = load_cases(case_path)
        fixture_bundle = build_fixture_bundle(DEFAULT_PERSONALITY, DEFAULT_RELATIONSHIP)
        entries = []
        for case_index, case in enumerate(cases, start=1):
            data_dir = worker_dir / f"case-{case_index:02d}"
            data_dir.mkdir(parents=True, exist_ok=True)
            _fixture_files(data_dir, fixture_bundle["personality"], fixture_bundle["relationship"])
            config.DATA_DIR = str(data_dir)
            os.environ["DATA_DIR"] = str(data_dir)
            with patch("time.time", return_value=FIXED_START.timestamp()):
                service = ChatService()
            fixture_life_event = seed_fixture_life_event(
                service.life, when=FIXED_START - dt.timedelta(seconds=90),
                text=FIXTURE_LIFE_TEXT)
            real_client = service.client
            real_chat_client = service.chat_client
            service.embed_client = SimpleNamespace(embeddings=SimpleNamespace(
                create=lambda **kwargs: (_ for _ in ()).throw(
                    AssertionError("embedding disabled in companionship evaluation"))))
            service.store.embed_client = service.embed_client
            service._advance_life = lambda: None
            service.run_evolve_pending = lambda: None
            history = copy.deepcopy(case.get("seed_history", []))
            try:
                for round_number, turn in enumerate(case["turns"], start=1):
                    question = str(turn["input"])
                    offset = (round_number - 1) * 60
                    if case["id"] == "隔日未完事" and round_number > 1:
                        offset += 86400
                    when = FIXED_START + dt.timedelta(seconds=offset)
                    now = when.timestamp()
                    slot = slot_of(when)
                    service.life._save_state(slot, when.date().isoformat(),
                                             {"place": "家里", "activity": "收好画具后坐着休息",
                                              "mood": "平静", "on_mind": ""},
                                             at=now, advanced_at=now,
                                             next_advance_at=now + 86400)
                    recorder = ModelRecorder(real_client if case.get("mode") == "proactive"
                                             else real_chat_client, live=live, gate=gate,
                                             request_timeout=request_timeout)
                    service.client = recorder
                    service.chat_client = recorder
                    product_output = None
                    generation_error = None
                    full_input = []
                    with patch("time.time", return_value=now), \
                            patch.object(clock, "now_text", return_value=clock.now_text(when)), \
                            patch.object(clock, "day_part", return_value=clock.day_part(when)):
                        try:
                            if case.get("mode") == "proactive":
                                lines = [("他：" if message["role"] == "user" else "她：")
                                         + message["content"] for message in history]
                                lines.append("他：" + question)
                                product_output = service.speaker.speak(
                                    recorder, config.EXTRACT_MODEL, chat_lines=lines, now=now)
                            else:
                                full_input = build_messages(service.system_prompt(question), history, question)
                                product_output = service.ask(full_input)
                        except Exception as exc:
                            generation_error = _error(exc)
                    latest = recorder.calls[-1] if recorder.calls else {}
                    entry = {"id": case["id"], "group": case["group"], "round": round_number,
                             "input": latest.get("input", full_input),
                             "raw_output": latest.get("raw_output"),
                             "raw_response": latest.get("raw_response"),
                             "product_output": product_output,
                             "model_params": latest.get("model_params", {}),
                             "calls": recorder.calls,
                             "error": generation_error or latest.get("error"),
                             "fixed_time": when.isoformat(), "fixture_history": case.get("seed_history", []),
                             "fixture": {"template": fixture_bundle["template"],
                                         "personality": fixture_bundle["personality"],
                                         "personality_hash": fixture_bundle["personality_hash"],
                                         "relationship_hash": fixture_bundle["relationship_hash"],
                                         "life_event": fixture_life_event,
                                         "life_event_origin": "synthetic_isolated_fixture",
                                         "relationship": {"stage": fixture_bundle["relationship"]["stage"],
                                                           "decide": fixture_bundle["relationship"]["decide"],
                                                           "since": fixture_bundle["relationship"]["since"]}},
                             "isolation": {"data_dir": str(data_dir),
                                           "blocked_network": list(gate.denied),
                                           "history_mode": "explicit_history_no_extraction"}}
                    entry["hash"] = canonical_hash(entry)
                    entries.append(entry)
                    if checkpoint is not None:
                        checkpoint.write_text(json.dumps({"entries": entries}, ensure_ascii=False),
                                              encoding="utf-8")
                    if live:
                        print(f"case={case_index}/12; round={round_number}/3; "
                              f"calls={len(recorder.calls)}; error={entry['error']['type'] if entry['error'] else 'none'}",
                              flush=True)
                    history.append({"role": "user", "content": question})
                    assistant_text = ("\n".join(str(part) for part in product_output)
                                      if isinstance(product_output, list) else str(product_output or ""))
                    if assistant_text:
                        history.append({"role": "assistant", "content": assistant_text})
            finally:
                service.close()
                for client in {real_client, real_chat_client}:
                    client.close()
        print(WORKER_MARKER + json.dumps({"entries": entries, "fixture": fixture_bundle}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(WORKER_MARKER + json.dumps({"error": _error(exc)}, ensure_ascii=False))
        return 1


def _error(exc: BaseException) -> dict[str, str]:
    message = re.sub(r"(?i)(api[_ -]?key|authorization|bearer)\s*[:=]\s*[^\s,;]+",
                     r"\1=<redacted>", str(exc))[:500]
    for name, value in os.environ.items():
        if len(value) >= 6 and any(fragment in name.lower() for fragment in SECRET_KEYS):
            message = message.replace(value, "<redacted>")
    return {"type": type(exc).__name__, "message": message}


def _run_snapshot(snapshot: Path, version: str, live: bool,
                  request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
                  worker_timeout: float = DEFAULT_WORKER_TIMEOUT) -> list[dict[str, Any]]:
    code_hash = _code_hash(snapshot)
    (OUTPUT_DIR / ".worker-data").mkdir(parents=True, exist_ok=True)
    checkpoint = OUTPUT_DIR / ".worker-data" / f"checkpoint-{version}-{uuid.uuid4().hex}.json"
    command = [sys.executable, str(Path(__file__).resolve()), "--worker",
               "--snapshot", str(snapshot), "--cases", str(CASES_PATH),
               "--request-timeout", str(request_timeout), "--checkpoint", str(checkpoint)]
    if live:
        command.append("--live")
    environment = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    process = subprocess.Popen(command, cwd=ROOT, text=True, encoding="utf-8", env=environment,
                               errors="replace", stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output: list[str] = []
    output_queue: queue.Queue[str | None] = queue.Queue()

    def read_output():
        assert process.stdout is not None
        for line in process.stdout:
            output_queue.put(line)
        output_queue.put(None)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    started = time.monotonic()
    while True:
        try:
            line = output_queue.get(timeout=0.25)
        except queue.Empty:
            if process.poll() is not None:
                break
            if time.monotonic() - started > worker_timeout:
                process.kill()
                process.wait()
                break
            continue
        if line is None:
            break
        output.append(line)
        if live and line.startswith("case="):
            print(f"[{version}] {line.strip()}", flush=True)
    if process.poll() is None:
        process.wait()
    timed_out = time.monotonic() - started > worker_timeout
    marker = next((line[len(WORKER_MARKER):] for line in reversed(output)
                   if line.startswith(WORKER_MARKER)), "")
    checkpoint_entries = []
    if checkpoint.exists():
        try:
            checkpoint_entries = json.loads(checkpoint.read_text(encoding="utf-8")).get("entries", [])
        except Exception:
            checkpoint_entries = []
    if timed_out and not marker:
        entries = recover_entries(load_cases(), checkpoint_entries,
                                  {"type": "worker_timeout", "message": f"worker exceeded {worker_timeout:g}s"})
        payload = {"entries": entries}
    elif not marker:
        raise RuntimeError(f"worker failed ({process.returncode}): {''.join(output)[-1000:]}")
    else:
        payload = json.loads(marker)
    if payload.get("error"):
        raise RuntimeError(payload["error"].get("message", "worker error"))
    changed_during_run = _code_hash(snapshot) != code_hash
    for entry in payload["entries"]:
        entry["version"] = version
        entry["code_hash"] = code_hash
        if changed_during_run:
            entry["error"] = {"type": "code_changed_during_run",
                              "message": "source files changed while worker ran; rerun after implementation stabilizes"}
        entry["hash"] = canonical_hash({key: value for key, value in entry.items() if key != "hash"})
    return payload["entries"]


def _write_outputs(entries: list[dict[str, Any]], live: bool) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = OUTPUT_DIR / "evaluation-report.json"
    order = build_blind_order(entries)
    for index, entry in enumerate(entries, start=1):
        entry["item_id"] = "item-" + canonical_hash({"seed": 193196, "index": index})[:16]
        entry["hash"] = canonical_hash({key: value for key, value in entry.items() if key != "hash"})
    paired = {}
    for entry in entries:
        paired.setdefault((entry["id"], entry["round"]), {})[entry["version"]] = entry
    comparison_errors = []
    for (case_id, round_number), pair in paired.items():
        if set(pair) != {"before", "current"}:
            comparison_errors.append(f"{case_id}/{round_number}: missing version")
        elif pair["before"]["model_params"] != pair["current"]["model_params"]:
            comparison_errors.append(f"{case_id}/{round_number}: model parameters differ")
    review_by_item = {entry["item_id"]: {
        "blind_id": next(item["blind_id"] for item in order if item["item_id"] == entry["item_id"]),
        "input": entry["input"], "raw_output": entry["raw_output"], "error": entry["error"],
    } for entry in entries}
    ordered_review = [review_by_item[item["item_id"]] for item in order]
    fixture_reference = next((entry.get("fixture") for entry in entries if entry.get("fixture")), {})
    raw_path.write_text(json.dumps({"live": live, "naturalness_score": None,
                                    "fixture_reference": fixture_reference,
                                    "comparison_errors": comparison_errors,
                                    "entries": entries}, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUT_DIR / "blind-order.json").write_text(json.dumps(order, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUT_DIR / "blind-review.json").write_text(json.dumps(ordered_review, ensure_ascii=False, indent=2), encoding="utf-8")
    versions = sorted({entry["version"] for entry in entries})
    errors = sum(bool(entry["error"]) for entry in entries)
    actual_calls = sum(len(entry.get("calls", [])) for entry in entries)
    error_counts = {}
    for entry in entries:
        if entry["error"]:
            kind = entry["error"]["type"]
            error_counts[kind] = error_counts.get(kind, 0) + 1
    report = ["# 自然相处隔离评测报告", "", f"模式：{'live' if live else 'offline（默认）'}",
              f"版本：{'、'.join(versions)}", f"条目：{len(entries)}（12 组 × 2 版本 × 3 轮）",
              f"错误/离线条目：{errors}", f"已捕获 API 尝试：{actual_calls}（离线尝试未发请求）",
              f"同模型同参数核对：{'一致' if not comparison_errors else '存在差异，见原始报告 comparison_errors'}",
              f"错误类型计数：{json.dumps(error_counts, ensure_ascii=False, sort_keys=True)}",
              "", "## 评测边界", "",
              "本工具不预设正确台词，也不计算自然度主观分。离线模式只生成临时目录中的完整上下文；",
              "只有显式传入 `--live` 才向当前配置的模型发请求。模型参数、原始输出、输入和哈希在 JSON 原始报告中逐条保存。",
              "已有入口的网络/格式重试会产生额外尝试；每次尝试的原始响应都保留在 calls，不把尝试合并成一次成功。",
              "主对话使用 ChatService.ask，主动分享案例使用 Speaker.speak；参数由入口实际请求捕获。",
              "两版均读取当前环境模型配置，固定隔离人格和同一生活记录；每组独立数据目录、固定时间、阻断额外网络。",
              "life_event 是两版相同的合成隔离夹具；实际落库的编号、时间、文本及 source 随条目保存，不能当作她真实经历。",
              "前一轮真实回复进入下一轮；seed_history 是明确标注的测试原话 fixture。离线没有伪造回复。",
              "连续性只验证提供给模型的历史文本；",
              "真实提炼、重启后的持久话题状态和生产数据均未在本报告中自动推断。", "",
              "## 文件", "", "- `evaluation-report.json`：逐条审计记录。",
              "- `blind-order.json`：固定种子生成的盲评顺序。",
              "- `blind-review.json`：按盲评顺序排列的完整输入和输出，不含版本字段。",
              "", "## 运行", "",
              "`D:\\conda\\envs\\test_py313\\python.exe benchmark\\evaluate_companionship.py` 生成离线审计；",
              "同一命令显式追加 `--live` 才运行真实模型多轮对照；live 默认单次请求超时 60 秒、单版 worker 超时 900 秒，",
              "每完成一轮会在标准输出报告 case/round/calls/error，并写入 checkpoint 以便超时恢复。"]
    (OUTPUT_DIR / "evaluation-report.md").write_text("\n".join(report) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="显式允许实际模型请求")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--snapshot", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--cases", type=Path, default=CASES_PATH, help=argparse.SUPPRESS)
    parser.add_argument("--request-timeout", type=float, default=DEFAULT_REQUEST_TIMEOUT,
                        help="单次模型请求超时秒数")
    parser.add_argument("--worker-timeout", type=float, default=DEFAULT_WORKER_TIMEOUT,
                        help="单版 worker 总超时秒数")
    parser.add_argument("--checkpoint", type=Path, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        if args.snapshot is None:
            parser.error("worker 需要 --snapshot")
        return _worker(args.snapshot, args.cases, args.live, args.request_timeout, args.checkpoint)
    cases = load_cases()
    current = ROOT
    before = SNAPSHOT_DIR
    if not (before / "app").is_dir():
        raise SystemExit(f"缺少 before 快照：{before / 'app'}（先运行 snapshot before）")
    entries = (_run_snapshot(before, "before", args.live, args.request_timeout, args.worker_timeout)
               + _run_snapshot(current, "current", args.live, args.request_timeout, args.worker_timeout))
    _write_outputs(entries, args.live)
    print(f"mode={'live' if args.live else 'offline'}; cases={len(cases)}; entries={len(entries)}")
    failures = sum(bool(entry["error"]) for entry in entries)
    print(f"error_entries={failures}; offline_skips={failures if not args.live else 0}")
    print(f"report={OUTPUT_DIR / 'evaluation-report.md'}")
    return int(args.live and failures > 0)


if __name__ == "__main__":
    raise SystemExit(main())

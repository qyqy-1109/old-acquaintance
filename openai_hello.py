"""命令行版 View：只管读输入、打输出，业务逻辑全在 app/services/chat_service.py。

重构后有两个 View，共用同一套 Service，行为完全一致：
    python openai_hello.py       命令行聊
    python web.py                浏览器聊（http://127.0.0.1:8765）

分层：
    app/views/index.html  View（网页）      本文件 View（命令行）
    app/controllers/      Controller        HTTP 进出口
    app/services/         Service           一轮对话怎么编排
    app/models/           Model             性格 / 世界书 / 画像 / 记忆

安全提醒：
    API key 从 .env 或环境变量读取，不要提交 .env、不要发给别人。

命令：
    /personality [描述|reset]  查看/修改/回滚性格模板
    /world [描述|del 序号]     查看/手写世界书
    /user [描述]               查看/补充用户画像
    /profile                   立即做一次画像分析
    /clear                     清空当前会话（长期记忆和世界书保留）
    /mem                       查看记忆统计
    /forget                    遗忘长期记忆（世界书不清）
    exit                       退出
"""

import sys

from app import config
from app.services.chat_service import ChatService


def interactive(service: ChatService) -> int:
    state = service.state()
    print(f"模型：{state['model']}　|　{state['personality']}")
    print(f"{state['memory']}　|　{state['profile']}")
    greeting = service.greeting()  # 由它触发生成；state 里只带缓存值
    if greeting:
        print(f"{state['name']}：{greeting}")
    if state.get("memory_issues"):
        print(f"⚠ 对账发现 {len(state['memory_issues'])} 处矛盾，看详情：/mem check")
    print("命令：/personality /world /user /profile /clear /mem，exit 退出")
    print("      /mem dead 看从没被想起的记忆　/mem check 跨层对账\n")

    while True:
        try:
            question = input("你：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出")
            return 0
        if not question:
            continue
        if question.lower() in config.EXIT_WORDS:
            print("已退出")
            return 0

        if service.is_command(question):
            print(service.run_command(question) + "\n")
            continue

        messages, recalled = service.prepare(question)
        if recalled:
            n_world = sum(1 for h in recalled if h["kind"] == "world")
            tail = f"，其中世界书 {n_world} 条" if n_world else ""
            print(f"[召回 {len(recalled)} 条{tail}，相似度 "
                  + "、".join(f"{h['sim']:.2f}" for h in recalled) + "]")

        print(f"{service.personality.display_name()}：", end="", flush=True)
        if config.STREAM:
            chunks = []
            for text in service.stream(messages):
                chunks.append(text)
                print(text, end="", flush=True)
            print()
            answer = "".join(chunks)
        else:
            answer = service.ask(messages)
            print(answer)

        after = service.finish_turn(question, answer)
        if after["memory_note"]:
            print(f"[记忆] {after['memory_note']}")
        if after["profile_note"]:
            print(f"[画像] {after['profile_note']}")
        print()


def main() -> int:
    if not config.API_KEY:
        print("错误：请先把 .env.example 复制成 .env，并填上 WB_API_KEY。")
        return 1

    service = ChatService()
    try:
        return interactive(service)
    except Exception as exc:
        print(f"\n调用失败：{type(exc).__name__}: {exc}")
        return 1
    finally:
        service.close()
        ok, msg = service.evolve_personality()
        if msg:
            print(f"[性格模板{'迭代失败，稍后重试' if not ok else '已迭代'}] {msg}")


if __name__ == "__main__":
    sys.exit(main())

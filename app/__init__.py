"""旧识对话助手。

分层（MVC + S）：

    app/views/         View       —— 看到的：浏览器页面 app/views/index.html、命令行 cli
    app/controllers/   Controller —— 接请求、出响应：FastAPI 路由，不含业务逻辑
    app/services/      Service    —— 编排：一句话要过哪几步、顺序是什么、失败怎么办
    app/models/        Model      —— 数据与领域规则：性格模板、世界书、画像、记忆
    app/config.py                 —— 全部常量集中在这里

为什么多拆一层 Service：Controller 只管 HTTP 的进出，CLI 也走同一套 Service，
这样"加个 Web 界面"不会把业务逻辑复制一份，两边行为永远一致。
"""

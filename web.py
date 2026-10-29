"""Web 版入口：启动本地服务，浏览器打开 http://127.0.0.1:8765

只监听 127.0.0.1，不对外暴露。想改端口改 app/config.py 里的 PORT。
"""

import uvicorn

from app import config

if __name__ == "__main__":
    print(f"旧识已启动 → http://{config.HOST}:{config.PORT}")
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, reload=False)

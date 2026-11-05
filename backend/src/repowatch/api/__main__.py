"""显式启动回环 API；浏览器片段接收一次引导，不将令牌写到日志。"""

import argparse
import asyncio
import webbrowser

import uvicorn

from repowatch.api.host import create_host_app
from repowatch.api.security import LocalSessions


def main():
    parser = argparse.ArgumentParser(description="RepoWatch 本地控制台；先显式完成数据库迁移")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口无效")
    origin = f"http://127.0.0.1:{args.port}"
    sessions = LocalSessions(origin)
    app = create_host_app(sessions=sessions)

    class BrowserServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            if self.started:
                webbrowser.open(origin + "/#session=" + sessions.bootstrap_token)

    server = BrowserServer(
        uvicorn.Config(app, host="127.0.0.1", port=args.port, proxy_headers=False)
    )
    asyncio.run(server.serve())


if __name__ == "__main__":
    main()

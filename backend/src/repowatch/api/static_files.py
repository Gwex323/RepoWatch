"""前端构建资源的固定类型，不继承宿主注册表对 .js 的错误映射。"""

from pathlib import Path

from fastapi.staticfiles import StaticFiles


class FrontendFiles(StaticFiles):
    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        suffix = Path(full_path).suffix.lower()
        types = {
            ".js": "text/javascript; charset=utf-8",
            ".mjs": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }
        if suffix in types:
            response.headers["content-type"] = types[suffix]
        return response

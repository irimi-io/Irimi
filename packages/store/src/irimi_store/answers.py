import json
from typing import Any

from starlette.responses import JSONResponse


class JsonAnswer(JSONResponse):
    def render(self, content: Any) -> bytes:
        return json.dumps(content, allow_nan=False, separators=(",", ":")).encode()

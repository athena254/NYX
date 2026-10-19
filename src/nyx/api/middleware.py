"""Request-id middleware: every response and every log line carries a request id."""

from __future__ import annotations

import logging
import re
import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from nyx.domain.ids import new_request_id
from nyx.observability.logging import request_id_var, set_request_id

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_logger = logging.getLogger("nyx.api.access")


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Attach a request id to ``request.state``, the response header, and the log context."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        supplied = request.headers.get("X-Request-Id")
        request_id = (
            supplied if supplied and _REQUEST_ID_PATTERN.match(supplied) else new_request_id()
        )
        request.state.request_id = request_id
        token = set_request_id(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = (time.perf_counter() - started) * 1000
            _logger.exception(
                "request failed",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": round(duration_ms, 2),
                },
            )
            raise
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-Id"] = request_id
        _logger.info(
            "request completed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        return response

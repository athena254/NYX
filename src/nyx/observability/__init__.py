"""Structured logging for NYX.

Logs carry a request id and never carry payload contents or secrets. The formatter emits one
JSON object per line so an operator can correlate a request without searching payloads.
"""

from nyx.observability.logging import configure_logging, request_id_var, set_request_id

__all__ = ["configure_logging", "request_id_var", "set_request_id"]

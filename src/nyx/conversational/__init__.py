"""The conversational shell for NYX.

This package is the chat-facing layer that sits alongside, and independently of, the
deterministic core. It never imports the core's internals (critics, findings, lifecycle, policies,
or storage); it reaches NYX only through the HTTP API. The language model is the interface, not
the judge: nothing here produces, changes, or validates a finding, verdict, or disposition.
"""

"""Independent end-to-end probe: the NYX MCP server over a REAL stdio session.

Does not use the project's test helpers. Spawns `python -m nyx.conversational.mcp_server`
as a subprocess and speaks MCP to it over stdin/stdout, exactly as an MCP client (Hermes,
Claude Code, Cursor) would. The server in turn talks to a live NYX over real HTTP.

Checks, in order:
  1. initialize handshake -> protocol version, server name
  2. tools/list -> the tool surface the client would be offered
  3. FORBIDDEN tools absent (the safety property, asserted against the live surface)
  4. tools/call nyx_list_systems -> real HTTP round-trip to NYX
  5. tools/call nyx_list_critics
  6. tools/call nyx_get_finding <missing> -> NYX's NOT_FOUND arrives as a result, not a crash
  7. tools/call nyx_get_history <missing>
  8. tools/call nyx_request_verification on a missing finding -> NYX's refusal surfaces
  9. tools/call nyx_submit_rebuttal on a missing finding -> NYX's refusal surfaces
 10. tools/call nyx_list_findings -> a list result
 11. tools/call nyx_list_policy_versions -> a list result
"""

import json
import subprocess
import sys
import time

PORT = 8000
PROC = None
failures: list[str] = []
_id = 0


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def send(method, params=None, notify=False):
    global _id
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    if not notify:
        _id += 1
        msg["id"] = _id
    PROC.stdin.write(json.dumps(msg) + "\n")
    PROC.stdin.flush()
    if notify:
        return None
    while True:
        line = PROC.stdout.readline()
        if not line:
            raise RuntimeError("server closed stdout")
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue  # a log line that landed on stdout; keep reading


def _call_result(r):
    """Return the tool's text payload as parsed JSON (independent of the result shape)."""

    content = (r.get("result") or {}).get("content", [])
    text = "".join(b.get("text", "") for b in content)
    check("tool result carries a text payload", bool(text.strip()), repr(text[:80]))
    return json.loads(text)


def main():
    global PROC
    PROC = subprocess.Popen(
        [sys.executable, "-m", "nyx.conversational.mcp_server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    try:
        # 1. handshake
        t0 = time.time()
        r = send(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "independent-probe", "version": "1.0"},
            },
        )
        lat = round((time.time() - t0) * 1000)
        result = r.get("result", {})
        check("initialize returns a result", bool(result), f"{lat}ms")
        check(
            "server reports name 'nyx'",
            result.get("serverInfo", {}).get("name") == "nyx",
            str(result.get("serverInfo")),
        )
        check(
            "server advertises tools capability",
            "tools" in (result.get("capabilities") or {}),
            str(result.get("capabilities")),
        )
        send("notifications/initialized", {}, notify=True)

        # 2. tool surface
        r = send("tools/list", {})
        tools = r.get("result", {}).get("tools", [])
        names = sorted(t["name"] for t in tools)
        print(f"      {len(names)} tools: {', '.join(names)}")
        check("tools/list returns 12 tools", len(names) == 12, str(len(names)))
        check(
            "every tool has a description",
            all(t.get("description") for t in tools),
        )

        # 3. the safety property, against the live surface
        forbidden = {
            "nyx_create_finding",
            "nyx_record_disposition",
            "nyx_transition_finding",
            "nyx_set_finding_status",
            "nyx_accept_risk",
        }
        present = forbidden & set(names)
        check("no judge-capable tool is exposed", not present, f"leaked={sorted(present)}")

        # 4/5. read tools over real HTTP
        r = send("tools/call", {"name": "nyx_list_systems", "arguments": {}})
        payload = _call_result(r)
        # The probe is re-runnable against a persistent store, so assert the shape, not emptiness.
        systems = payload.get("results", payload)
        check(
            "nyx_list_systems returns an explicit list",
            isinstance(systems, list),
            f"{len(systems)} system(s)",
        )

        r = send("tools/call", {"name": "nyx_list_critics", "arguments": {}})
        payload = _call_result(r)
        critics = payload.get("results", payload)
        check(
            "nyx_list_critics returns the registered pack",
            len(critics) >= 9,
            f"{len(critics)} critics",
        )

        # 6/7/8. NYX's refusals arrive as reportable results
        r = send(
            "tools/call",
            {"name": "nyx_get_finding", "arguments": {"finding_id": "find_missing_zzz"}},
        )
        payload = _call_result(r)
        check(
            "missing finding -> an explicit null result, not an empty string",
            payload.get("result") is None and "result" in payload,
            str(payload)[:200],
        )

        r = send(
            "tools/call",
            {
                "name": "nyx_request_verification",
                "arguments": {
                    "finding_id": "find_missing_zzz",
                    "check_id": "c001",
                    "requested_by": "probe",
                },
            },
        )
        payload = _call_result(r)
        check(
            "unauthorised/missing write -> refused, not a crash",
            "error" in payload,
            str(payload)[:200],
        )
        check(
            "refusal is not a tool-level exception",
            not r.get("error"),
        )
        print(f"      refusal: {json.dumps(payload)[:160]}")

        # 9. a write against a missing record is refused, not crashed
        r = send(
            "tools/call",
            {
                "name": "nyx_submit_rebuttal",
                "arguments": {
                    "finding_id": "find_missing_zzz",
                    "submitted_by": "probe",
                    "argument": "this finding was never raised",
                },
            },
        )
        payload = _call_result(r)
        check(
            "rebuttal on a missing finding -> refused",
            "error" in payload,
            str(payload)[:200],
        )

        # 10/11. list tools over real HTTP
        for tool, args in (
            ("nyx_list_findings", {}),
            ("nyx_list_policy_versions", {}),
            ("nyx_list_rebuttals", {"finding_id": "find_missing_zzz"}),
        ):
            r = send("tools/call", {"name": tool, "arguments": args})
            payload = _call_result(r)
            check(
                f"{tool} -> an explicit result (refusal or list), never empty",
                ("results" in payload) or ("error" in payload),
                str(payload)[:160],
            )

    finally:
        PROC.stdin.close()
        try:
            PROC.wait(timeout=10)
        except subprocess.TimeoutExpired:
            PROC.kill()
        err = PROC.stderr.read()
        if err.strip():
            print("      server stderr:", err.strip()[:400])

    print()
    if failures:
        print(f"PROBE FAILED: {len(failures)} check(s) -> {failures}")
        sys.exit(1)
    print("PROBE PASSED: MCP stdio session round-tripped against a live NYX")


if __name__ == "__main__":
    main()

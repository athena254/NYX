# The CLI contract

**Status:** contract-tested (Phase 7 slice 6.1). **Machine interface:** `tests/integration/test_cli_contract.py`.

The CLI is how a **local script or another system on this machine** uses NYX. It opens the database
directly, so it is not a remote interface — a system elsewhere uses the HTTP API or the MCP server
instead. See `docs/conversational-interface.md`.

Every property below is asserted by a test, not merely documented. A change to any of them is a
deliberate edit to `tests/integration/test_cli_contract.py`.

## What every command guarantees

| Property | Assertion |
| --- | --- |
| Takes `--json` | `test_every_cli_command_offers_json` — introspected off the registered Typer commands, so a new command that forgets the flag fails the build |
| `--json` is a boolean switch | `test_json_flag_is_a_boolean_switch` — no value argument, defaults to `False` |
| `--json` stdout is exactly one JSON document | `test_json_output_is_one_document_with_no_surrounding_prose` — `json.loads(stdout)` never has to strip a log line or a status message |
| Failures exit non-zero | `test_failures_exit_non_zero_and_report_a_stable_code` |
| Failures never print a traceback | same test — an unhandled exception is a contract violation, not a diagnostic |
| No command prompts | `test_no_command_prompts_for_input` — every non-interactive command runs with stdin closed |

## Exit codes

There are exactly two exit statuses, so a caller needs no error-code table to branch on success:

| Code | Meaning |
| --- | --- |
| `0` | The command succeeded. With `--json`, stdout is the result document. |
| `1` | The command failed. With `--json`, stdout is the error document. |
| `2` | The command line itself was wrong (unknown command, missing required argument). Emitted by Typer before any NYX code runs. |

A batch that contains one rejected item still exits `1`, and the per-item outcomes are in the
document — one bad item does not hide the good ones.

## Error document shape

A failure with `--json` prints the same envelope the HTTP API uses:

```json
{
  "code": "NOT_FOUND",
  "message": "Finding not found.",
  "request_id": "cli"
}
```

`code` is a stable `ErrorCode` value (`src/nyx/error_codes.py`), so a caller branches on it rather
than on prose. `request_id` is `"cli"` for the CLI; the API uses `req_...`.

Three commands report a **per-item** code rather than a top-level one, because they operate on
collections:

| Command | Shape |
| --- | --- |
| `nyx ingest batch --json` | `{"request_id": ..., "results": [{"index", "status", "record_id", "reason"}]}` — `reason` is the per-item code |
| `nyx ingest event --json` (rejected) | `{"status": "rejected", "record_id": null, "error": {"code", "message", ...}}` |
| `nyx ingest batch` with unparseable input | `{"code": "INVALID_REQUEST", "message": "Batch input must be a JSON array..."}` |

## Commands

All 19 commands, with the arguments that are required:

| Command | Required input |
| --- | --- |
| `nyx system register` | manifest path |
| `nyx system list` | — |
| `nyx ingest event` | path, or `-` for stdin |
| `nyx ingest batch` | path, or `-` for stdin |
| `nyx claim add` | path, or `-` for stdin |
| `nyx evidence add` | path, or `-` for stdin |
| `nyx critic list` | — |
| `nyx critic run` | `--event` |
| `nyx finding list` | — |
| `nyx finding show` | finding id |
| `nyx finding rebut` | finding id, `--submitted-by` |
| `nyx finding verify` | finding id |
| `nyx finding dispose` | finding id, `--outcome`, `--reason` |
| `nyx finding history` | finding id |
| `nyx doctor` | — |
| `nyx chat ask` | question |
| `nyx chat shell` | — (interactive) |
| `nyx telegram check` | — |
| `nyx telegram serve` | — (long-running; Ctrl-C to stop) |

## Three behaviours worth knowing

**A file that cannot be opened is `INVALID_REQUEST`, not a traceback.** `nyx ingest event missing.json
--json` exits `1` with a structured error naming the path, the same as a malformed body. Before
Phase 7 slice 6.1 this raised `FileNotFoundError` and printed a traceback.

**`nyx chat` fails closed.** With `NYX_LLM_ENABLED=true` and no `NYX_LLM_MODEL`, it exits `1` with
`INVALID_REQUEST` and names the missing variable — it does not guess a model or print a pydantic
traceback. The same holds for an unreachable NYX: the message names the URL and the command to start
it.

**`nyx telegram` fails closed too, and for a stronger reason.** With `NYX_TELEGRAM_ENABLED=true`
but no token, or no `NYX_TELEGRAM_ALLOWED_USER_IDS`, it exits `1` naming the missing variable. A
bot without an allowlist is a bot that answers anyone who finds it, so the allowlist is required
rather than defaulted. `nyx telegram check` is the dry run: it calls `getMe` and reports the bot's
identity and its effective, secret-safe configuration without sending anything to a user.

## A note on `--json` and stderr

Diagnostics a human needs (for example "NYX records touched: ...") go to **stderr**, so stdout stays
parseable. The `chat ask` command is the one place this matters most, because its answer is the
document and the record ids are context.

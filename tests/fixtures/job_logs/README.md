# Job log fixtures

These are synthetic offline observations. The repository, run, job, log text,
token-like marker, and signed-URL query strings in the tests are invented.
No downloaded Actions log or actual credential is included.

`observation.json` covers an exact rerun attempt, nonconsecutive observed step
numbers, UTF-8 text, a leading BOM, ANSI color escapes, OSC hyperlinks, and a
redaction value split by ANSI escapes. The expected text is already sanitized.

The tests use bounded in-memory response objects. They do not contact GitHub,
write retrieved bodies, or attach raw logs to JUnit output. Error paths assert
static error codes and the absence of fixture markers in diagnostics.

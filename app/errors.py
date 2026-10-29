"""Cross-module control-flow signals for the fixed checkup workflow.

``ProviderStopError`` is not a contract type: it carries the reason why the
whole run must stop before issuing any further provider HTTP. It is raised for
the local request/time budget and for a confirmed provider quota exhaustion.

Two rules matter for correctness:

* it must never be converted into a retryable provider timeout (no sleep, no
  retry, no further request), and
* generic ``except Exception`` recovery must not swallow it into "unknown"
  evidence, because doing so lets the run keep spending HTTP after a stop.
"""


class ProviderStopError(RuntimeError):
    """Stop the current run immediately: no further provider requests."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)

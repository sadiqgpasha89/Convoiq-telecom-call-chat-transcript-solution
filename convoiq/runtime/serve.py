"""Single-process dev server: every service wired over the in-memory bus, gateway on ``/``, the others mounted under ``/_<name>``.

    python -m convoiq.runtime.serve --port 8080          # or: make run-local

NOT a production topology (one process, in-memory state, simulated BSS/OSS) - it exists so the full pipeline can be exercised and demoed
without Docker or a GCP project.  Auth is disabled unless CONVOIQ_AUTH_MODE is set explicitly.
"""
from __future__ import annotations

import argparse
import os

from convoiq.config import Settings


def build_app(settings: Settings | None = None):
    from convoiq.runtime.local import LocalSystem
    s = settings or Settings.from_env()
    if "CONVOIQ_AUTH_MODE" not in os.environ:
        s = s.model_copy(update={"auth_mode": "none"})
    s = s.model_copy(update={"env": "local", "connector_mode": "simulated"})
    system = LocalSystem(s)
    app = system.gateway.app
    for name, svc in (("sim", system.sim), ("broker", system.broker), ("monitor", system.monitor), ("orchestrator", system.orchestrator),
                      ("redaction", system.redaction), ("analytics", system.analytics), ("enrichment", system.enrichment)):
        app.mount(f"/_{name}", svc.app)
    app.state.system = system
    return app


def main() -> None:
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args()
    uvicorn.run(build_app(), host=a.host, port=a.port, log_level="info")


if __name__ == "__main__":
    main()

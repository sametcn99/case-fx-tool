"""HTTP surface for the currency conversion tool."""

from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="fx-tool", version="0.1.0")


@app.get("/health")
async def health() -> dict:
    """Liveness only.

    This deliberately does not probe the upstream: a broken upstream does not
    mean this process should be restarted, and an orchestrator should not take
    us down because the ECB feed is having a bad morning.
    """
    return {"ok": True}

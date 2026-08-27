"""Reading the run log, and judging it.

`POST /runs/{id}/verdict` is the endpoint the accuracy numbers depend on. Spec §9 item 17:
a run nobody has judged is UNREVIEWED, and unreviewed is not correct — `GET /stats` reports
`reviewed` and `unreviewed` alongside every count so a rate can never be read off a window
nobody looked at. That is also why the verdict vocabulary is closed: a free-text verdict
column aggregates into nothing, and "looks fine" would quietly become a success.

These rows are read *here*, by a human asking how the agent did. They are never read by the
graph during triage (import-linter enforces it), because a knowledge base that retrieves its
own raw attempts buries the one useful entry under every attempt to find it.
"""

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.storage.records import VERDICTS, RecordStore

router = APIRouter()

NO_STORE = (
    "No DATABASE_URL is configured, so no runs are recorded. Triage still works; "
    "evaluation does not."
)


class VerdictRequest(BaseModel):
    verdict: str = Field(description=f"One of: {', '.join(VERDICTS)}")
    note: str | None = Field(default=None, description="What actually happened")


def _store(request: Request) -> RecordStore:
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(status_code=503, detail=NO_STORE)
    return store


@router.get("/runs")
async def list_runs(
    request: Request,
    limit: int = Query(20, ge=1, le=200),
    alert: str | None = Query(None, description="Filter by alert name"),
) -> dict[str, Any]:
    rows = await _store(request).recent(limit=limit, alert_name=alert)
    return {"runs": rows, "count": len(rows)}


@router.get("/runs/{run_id}")
async def get_run(request: Request, run_id: int) -> dict[str, Any]:
    """One run with every step it executed — why it went the way it did."""
    run = await _store(request).replay(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no run #{run_id}")
    return run


@router.post("/runs/{run_id}/verdict")
async def set_verdict(request: Request, run_id: int, body: VerdictRequest) -> dict[str, Any]:
    if body.verdict not in VERDICTS:
        raise HTTPException(
            status_code=400, detail=f"verdict must be one of {', '.join(VERDICTS)}"
        )
    if not await _store(request).set_verdict(run_id, body.verdict, body.note):
        raise HTTPException(status_code=404, detail=f"no run #{run_id}")
    return {"id": run_id, "verdict": body.verdict, "note": body.note}


@router.get("/stats")
async def stats(request: Request, days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    """Aggregates over the window, with unreviewed runs excluded from every rate."""
    data = await _store(request).stats(days=days)
    if not data:
        raise HTTPException(status_code=503, detail="the record store did not answer")
    return {"days": days, **data}

"""Which deployment serves an alert.

The server binary is one artifact deployed as several path-scoped deployments, so mapping
an alert's host/path onto a workload is a lookup, not a search. The lookup is cheap; what
is expensive is a lookup that guesses and does not say so. A catch-all match returns a real
deployment whose pod and replica queries succeed and return true data about the wrong
workload — the failure renders exactly like a success. Hence every answer carries its
standing (`confidence`, `matched_by`, `note`) and only an explicit app label or a real path
prefix is allowed to call itself exact.
"""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from app.domain.models import Resolution

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "deployments.yaml"

CATCH_ALL_PREFIX = "/"


class Deployment(BaseModel):
    """One deployment shard of the server binary.

    The four naming dimensions get conflated constantly, so all of them are carried
    explicitly rather than collapsed into a single ambiguous "service name".
    """

    app_label: str
    pod_pattern: str
    hosts: list[str] = Field(default_factory=list)
    path_prefixes: list[str] = Field(default_factory=list)
    replicas: int | None = None
    traffic_share: str | None = None
    code_route_paths: list[str] = Field(default_factory=list)


def _load() -> list[Deployment]:
    if not CONFIG_PATH.is_file():
        return []
    raw = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    return [Deployment(**body) for body in (raw.get("deployments") or [])]


DEPLOYMENTS: list[Deployment] = _load()


def deployment_for(app_label: str | None) -> Deployment | None:
    """The full record behind a resolution, for callers that need more than the flat view."""
    if not app_label:
        return None
    return next((d for d in DEPLOYMENTS if d.app_label == app_label), None)


def _resolved(
    deployment: Deployment, *, confidence: str, matched_by: str, note: str = ""
) -> Resolution:
    return Resolution(
        app_label=deployment.app_label,
        pod_pattern=deployment.pod_pattern,
        hosts=list(deployment.hosts),
        replicas=deployment.replicas,
        traffic_share=deployment.traffic_share,
        code_route_paths=list(deployment.code_route_paths),
        confidence=confidence,
        matched_by=matched_by,
        note=note,
    )


def resolve(
    host: str | None = None, path: str | None = None, app_label: str | None = None
) -> Resolution:
    """Resolve a host/path/label onto a deployment, and say how the answer was reached.

    Only an explicit app label or a real path prefix counts as exact. Landing on the
    catch-all, or matching a host with nothing else to narrow it, is a guess and is
    returned as one.
    """
    if app_label:
        found = deployment_for(app_label)
        if found:
            return _resolved(found, confidence="exact", matched_by="app_label")
        # A label naming a workload the table does not know is not a reason to hand back
        # some other workload.
        return Resolution(
            matched_by="app_label",
            note=f"no deployment named {app_label!r}; it may not be in the shard table",
        )

    candidates = DEPLOYMENTS
    if host:
        by_host = [d for d in candidates if host in d.hosts]
        if not by_host:
            return Resolution(matched_by="host", note=f"no deployment serves host {host!r}")
        candidates = by_host

    if path:
        best, best_prefix = None, ""
        for d in candidates:
            for prefix in d.path_prefixes:
                if path.startswith(prefix) and len(prefix) > len(best_prefix):
                    best, best_prefix = d, prefix

        if best and best_prefix != CATCH_ALL_PREFIX:
            return _resolved(
                best, confidence="exact", matched_by="host+path" if host else "path"
            )
        if best:
            return _resolved(
                best,
                confidence="low",
                matched_by="catch-all",
                note=(
                    f"path {path!r} matched no deployment prefix and fell through to the "
                    f"catch-all; {best.app_label} may not serve it"
                ),
            )
        return Resolution(matched_by="path", note=f"path {path!r} matched no deployment prefix")

    if host:
        return _resolved(
            candidates[0],
            confidence="low",
            matched_by="host-only",
            note=(
                f"resolved from host {host!r} with no path to narrow it; "
                f"{host} is served by {len(candidates)} deployment(s)"
                if len(candidates) > 1
                else f"resolved from host {host!r} with no path to narrow it"
            ),
        )

    return Resolution(note="no host, path, or app label to resolve from")


def resolve_blast_radius(code_path: str) -> list[Deployment]:
    """Inverse direction: which deployments run this code."""
    return [d for d in DEPLOYMENTS if any(code_path.startswith(p) for p in d.code_route_paths)]

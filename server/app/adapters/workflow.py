import re
import httpx
from app.config import settings
from app.errors import AppError

REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}$")

def dispatch_ingestion() -> None:
    repo = settings.worker_dispatch_repo
    if not REPO_RE.fullmatch(repo) or not settings.worker_dispatch_token:
        raise AppError("worker_unconfigured", "Ingestion worker is not configured.", 503)
    url = f"https://api.github.com/repos/{repo}/actions/workflows/ingest.yml/dispatches"
    try:
        with httpx.Client(timeout=httpx.Timeout(10, connect=5), follow_redirects=False) as client:
            with client.stream("POST", url, headers={
                "Authorization": f"Bearer {settings.worker_dispatch_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
            }, json={"ref": settings.worker_dispatch_ref}) as response:
                if response.status_code != 204:
                    raise AppError("worker_dispatch_failed", "Ingestion worker could not be started.", 503)
    except httpx.RequestError:
        raise AppError("worker_dispatch_failed", "Ingestion worker could not be started.", 503)

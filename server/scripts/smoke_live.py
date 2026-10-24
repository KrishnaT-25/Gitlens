"""Run one real local GitHub → PostgreSQL → Gemini request path.

Run from server/ after setting .env and applying migrations:
    python -m scripts.smoke_live
"""

from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.services import auth, jobs


def expect(response, status=200):
    if response.status_code != status:
        raise RuntimeError(f"API returned {response.status_code}: {response.text[:500]}")
    return response.json()


def main():
    codes = []
    original_sender = auth.send_code
    auth.send_code = lambda _email, code: codes.append(code)
    try:
        with TestClient(app) as client:
            expect(client.get("/readyz"))
            email = f"smoke-{uuid4().hex[:12]}@example.com"
            challenge = expect(client.post("/api/auth/code", json={"email": email}))
            assert len(codes) == 1
            session = expect(client.post("/api/auth/verify", json={"challengeId": challenge["challengeId"], "code": codes.pop()}))
            headers = {"Authorization": f"Bearer {session['token']}"}
            expect(client.get("/api/auth/me", headers=headers))
            submitted = expect(client.post("/api/repositories", json={"url": "https://github.com/octocat/Hello-World"}, headers=headers))
            snapshot_id = submitted["snapshotId"]
            with SessionLocal() as db:
                claimed = jobs.claim(db)
            if claimed:
                jobs.process(*claimed)
            status = expect(client.get(f"/api/snapshots/{snapshot_id}/status", headers=headers))
            if status["status"] != "ready":
                raise RuntimeError(f"Snapshot ended in {status['status']}: {status['errorCode']}")
            summary = expect(client.get(f"/api/snapshots/{snapshot_id}/summary", headers=headers))
            assert summary["summary"]["purpose"]
            answer = expect(client.post(f"/api/snapshots/{snapshot_id}/chat", json={"question": "What does the README say?"}, headers=headers))
            assert answer["citations"] and not answer["insufficientEvidence"]
            assert all(f"/blob/{status['sha']}/" in citation["url"] for citation in answer["citations"])
            expect(client.post("/api/auth/logout", headers=headers))
            expect(client.get("/api/auth/me", headers=headers), 401)
            print(f"Live smoke passed: auth, snapshot {status['sha'][:8]}, summary, cited answer, logout.")
    finally:
        auth.send_code = original_sender


if __name__ == "__main__":
    main()

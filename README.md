# Kompressor

A compact, seed-backed media inventory and compression WebUI using FastAPI,
Jinja2, vanilla JavaScript and CSS. No frontend build step or external APIs.

## Run locally

Requires Python 3.12 or newer:

```sh
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. Run tests with `.venv/bin/python -m pytest`.
Run a **single application process** while using the in-memory fake queue.

## Seed workflows

- Movies and Shows display the existing JSON inventory. Episode tables are flat,
  with season separators, search, filters and mass selection.
- Review opens scoped presets and reads `/api/eligibility` for each selected item.
  Technical settings are read-only. Preserve Audio and container metadata are the
  only per-job options; the backend applies inherited protection tags.
- Queue submissions re-evaluate every item. Blocked, missing or already pending
  items are excluded individually. Encoder settings are copied from the preset,
  never accepted as client overrides.
- CPU and QSV are independent fake lanes. Each starts its next job on the next
  one-second scheduler tick. Encoding takes 180 simulated seconds, followed by
  five seconds of validation. Completed/skipped jobs appear in History.
- Default order is estimated bytes saved, descending. Manual priority overrides
  this order; Move next overrides priority within the same lane. Changing a job’s
  priority clears its Move next override. Active jobs require Stop & Skip.
- Queue/history live in memory, loaded from `fixtures/queue.json` on startup and
  reset on restart. No fixture or media file is written. Savings remain estimates;
  completing a fake job does not change the inventory.
- Settings shows the existing movie/show presets separately; editing is deferred.

## Architecture

The existing `PolicyEngine` remains the source of eligibility decisions.
`CatalogService` shares media lookup and tag inheritance between the API, views
and scheduler. Server-rendered estimates use the first eligible scoped preset,
falling back to the first enabled preset to expose blockers. The modal opens the
suggested preset and calls the eligibility API again when inputs change.

`QueueService` coordinates ordering, state transitions and duplicate prevention
under a lock. `FakeQueueRepository` owns session state; `FakeEncoderWorker` advances
progress independently of browser polling through the application lifespan.
Repository/worker protocols provide the future filesystem and encoder adapter
boundaries. There are no development-mode branches, databases, authentication,
media scans, hardware checks or ffmpeg dependencies.

New endpoints: `GET/POST /api/queue`, `DELETE /api/queue/{id}`,
`PATCH /api/queue/{id}/priority`, `POST /api/queue/{id}/move-next`, and
`POST /api/queue/{id}/skip`. Interactive API documentation is at `/docs`.

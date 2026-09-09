import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ar_pipeline.worker import build_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO)
    scheduler = build_scheduler()
    scheduler.start()
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title="AR pipeline", lifespan=lifespan)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}

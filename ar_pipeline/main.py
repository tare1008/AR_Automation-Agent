import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ar_pipeline.review.app import STATIC_DIR
from ar_pipeline.review.app import router as review_router
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

app.include_router(review_router)
app.mount("/review/static", StaticFiles(directory=str(STATIC_DIR)), name="review-static")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}

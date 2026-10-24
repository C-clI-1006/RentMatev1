"""RentMate backend entrypoint (thin wiring only).

Business logic lives in the domain modules; this file only creates the
app and mounts routers. Do not add business rules here.
"""

from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv(Path(__file__).with_name(".env"))

app = FastAPI(title="RentMate API", version="0.1.0")

from maintenance.api import router as maintenance_router

app.include_router(maintenance_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# Routers are mounted as modules land their first endpoints, e.g.:
# from communication import router as communication_router
# app.include_router(communication_router, prefix="/api")

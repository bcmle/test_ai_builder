"""Process health API. Product/run endpoints land with their complete workflows."""

from contextlib import asynccontextmanager
import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .config import load_config
from .store import Store


def create_app(data_dir=None, config_path=None):
    data = Path(data_dir or os.environ.get("AI_BUILDER_DATA", "data"))
    config = Path(config_path or os.environ.get("AI_BUILDER_CONFIG", "config/baseline.json"))

    @asynccontextmanager
    async def lifespan(app):
        app.state.config = load_config(config)
        app.state.store = Store(data / "app.sqlite3")
        yield

    app = FastAPI(title="AI Builder", version="0.1.0", lifespan=lifespan)

    @app.get("/api/v1/health/live")
    def live():
        return {"status": "alive"}

    @app.get("/api/v1/health/ready")
    def ready():
        try:
            load_config(config)
            with app.state.store.transaction() as db:
                db.execute("CREATE TEMP TABLE readiness (value INTEGER)")
                db.execute("INSERT INTO readiness VALUES (1)")
            # Probe the actual persistent volume; no provider request.
            import tempfile
            with tempfile.TemporaryFile(dir=data) as probe:
                probe.write(b"ready")
                probe.flush()
                os.fsync(probe.fileno())
            return {"status": "ready", "schema_version": 1}
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            return JSONResponse({"code": "not_ready", "message": "Storage or configuration unavailable"}, status_code=503)

    return app


app = create_app()

"""FastAPI application: REST endpoints, WebSocket updates, and the dashboard.

The server is a thin shell over ``AnalysisEngine``. It binds to localhost by
default — this is a personal tool reading your own screen, and there is no
reason for it to be reachable from the network.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .backtesting import Backtester
from .chart_detection.csv_source import load_csv
from .config import Config, load_config
from .engine import AnalysisEngine
from .logging_setup import get_logger, setup_logging
from .models import format_duration

log = get_logger(__name__)

DASHBOARD_DIR = Path(__file__).parent / "dashboard"

DISCLAIMER = (
    "This tool performs technical analysis and provides decision support only. "
    "It does not place trades, and it cannot know what the market will do next. "
    "Every signal is probabilistic; most market conditions should result in WAIT. "
    "Trading binary options carries a high risk of losing money."
)


class SettingsUpdate(BaseModel):
    asset: str | None = None
    chart_timeframe: int | None = Field(default=None, gt=0)
    trade_duration: int | None = Field(default=None, gt=0)
    min_confidence: float | None = Field(default=None, ge=0, le=100)
    min_duration_compatibility: float | None = Field(default=None, ge=0, le=100)
    alerts_enabled: bool | None = None
    alert_cooldown: float | None = Field(default=None, ge=0)
    poll_seconds: float | None = Field(default=None, gt=0)


class BacktestRequest(BaseModel):
    csv_path: str | None = None
    asset: str | None = None
    trade_duration: int = Field(default=180, gt=0)
    chart_timeframe: int | None = Field(default=None, gt=0)
    window: int = Field(default=250, ge=60, le=2000)
    step: int = Field(default=1, ge=1, le=50)
    use_recommended_duration: bool = False
    min_confidence: float | None = Field(default=None, ge=0, le=100)
    synthetic_candles: int | None = Field(default=None, ge=200, le=20000)


class NoteRequest(BaseModel):
    notes: str = ""


class ConnectionManager:
    """Tracks WebSocket clients and fans state updates out to them."""

    def __init__(self) -> None:
        self.active: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self.active.discard(websocket)

    def push_threadsafe(self, payload: dict[str, Any]) -> None:
        """Called from the engine thread; hands work to the event loop."""
        if self._loop is None or not self.active:
            return
        try:
            asyncio.run_coroutine_threadsafe(self.broadcast(payload), self._loop)
        except RuntimeError:  # pragma: no cover - loop shutting down
            pass

    async def broadcast(self, payload: dict[str, Any]) -> None:
        if not self.active:
            return
        message = json.dumps(payload, default=str)
        dead: list[WebSocket] = []
        for websocket in list(self.active):
            try:
                await websocket.send_text(message)
            except Exception:
                dead.append(websocket)
        for websocket in dead:
            self.disconnect(websocket)


def create_app(config: Config | None = None, autostart: bool = True) -> FastAPI:
    """Build the application. ``autostart=False`` is used by the tests."""
    config = config or load_config()
    setup_logging(
        level=str(config.get("logging.level", "INFO")),
        file=config.resolve_path("logging.file"),
    )

    engine = AnalysisEngine(config)
    connections = ConnectionManager()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        connections.bind_loop(asyncio.get_running_loop())
        engine.subscribe(connections.push_threadsafe)
        engine.subscribe_alerts(connections.push_threadsafe)
        if autostart:
            engine.start()
        try:
            yield
        finally:
            engine.unsubscribe(connections.push_threadsafe)
            engine.unsubscribe_alerts(connections.push_threadsafe)
            engine.close()

    app = FastAPI(
        title="Pocket Option Technical Analysis Assistant",
        description=DISCLAIMER,
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.engine = engine
    app.state.config = config

    # -- API -------------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "running": engine.state.running,
            "source": engine.source.name,
            "last_update": engine.state.last_update,
            "last_error": engine.state.last_error,
        }

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        return engine.snapshot()

    @app.get("/api/options")
    def options() -> dict[str, Any]:
        return engine.options()

    @app.get("/api/disclaimer")
    def disclaimer() -> dict[str, str]:
        return {"disclaimer": DISCLAIMER}

    @app.post("/api/settings")
    def update_settings(update: SettingsUpdate) -> dict[str, Any]:
        changes = {k: v for k, v in update.model_dump().items() if v is not None}
        if not changes:
            return {"applied": {}, "options": engine.options()}
        try:
            applied = engine.update_settings(changes)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"applied": applied, "options": engine.options()}

    @app.post("/api/engine/start")
    def start_engine() -> dict[str, Any]:
        engine.start()
        return {"running": True}

    @app.post("/api/engine/stop")
    def stop_engine() -> dict[str, Any]:
        engine.stop()
        return {"running": False}

    @app.post("/api/engine/tick")
    def manual_tick() -> dict[str, Any]:
        """Run one cycle on demand — handy when the loop is stopped."""
        engine.tick()
        return engine.snapshot()

    @app.get("/api/journal")
    def journal(
        limit: int = Query(default=50, ge=1, le=500),
        asset: str | None = None,
    ) -> dict[str, Any]:
        return {"entries": engine.journal.recent(limit=limit, asset=asset)}

    @app.get("/api/journal/{signal_id}")
    def journal_entry(signal_id: str) -> dict[str, Any]:
        entry = engine.journal.get(signal_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="signal not found")
        return entry

    @app.post("/api/journal/{signal_id}/notes")
    def annotate(signal_id: str, request: NoteRequest) -> dict[str, Any]:
        if not engine.journal.annotate(signal_id, request.notes):
            raise HTTPException(status_code=404, detail="signal not found")
        return {"ok": True}

    @app.get("/api/statistics")
    def statistics(asset: str | None = None) -> dict[str, Any]:
        return engine.journal.statistics(asset=asset)

    @app.get("/api/alerts")
    def alerts(limit: int = Query(default=25, ge=1, le=100)) -> dict[str, Any]:
        return {"alerts": engine.alerts.recent_alerts(limit)}

    @app.post("/api/backtest")
    def backtest(request: BacktestRequest) -> dict[str, Any]:
        from .chart_detection import generate_series
        from .signals import GateSettings

        if request.synthetic_candles:
            series = generate_series(
                request.synthetic_candles,
                symbol=request.asset or "SYNTHETIC",
                timeframe_seconds=request.chart_timeframe or engine.chart_timeframe,
            )
        else:
            path = request.csv_path or str(config.resolve_path("capture.csv_path"))
            try:
                series = load_csv(
                    path,
                    request.chart_timeframe,
                    request.asset or engine.asset,
                )
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        settings = engine.gate_settings()
        if request.min_confidence is not None:
            settings.min_confidence = request.min_confidence

        backtester = Backtester(settings=settings, window=request.window)
        result = backtester.run(
            series,
            trade_duration=request.trade_duration,
            asset=request.asset or series.symbol,
            step=request.step,
            use_recommended_duration=request.use_recommended_duration,
        )
        payload = result.to_dict()
        # The trade list can be long; the dashboard only charts the tail.
        payload["trades"] = payload["trades"][-200:]
        return payload

    # -- WebSocket ---------------------------------------------------------

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await connections.connect(websocket)
        try:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "state",
                        "state": engine.snapshot(),
                        "options": engine.options(),
                        "disclaimer": DISCLAIMER,
                    },
                    default=str,
                )
            )
            while True:
                # The client sends pings; we do not expect commands over the
                # socket, so anything received is simply acknowledged.
                await websocket.receive_text()
                await websocket.send_text(json.dumps({"type": "pong"}))
        except WebSocketDisconnect:
            connections.disconnect(websocket)
        except Exception:  # pragma: no cover - transport level
            connections.disconnect(websocket)

    # -- dashboard ---------------------------------------------------------

    if DASHBOARD_DIR.exists():
        app.mount(
            "/static", StaticFiles(directory=str(DASHBOARD_DIR)), name="static"
        )

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(str(DASHBOARD_DIR / "index.html"))

    else:  # pragma: no cover - only if the package is installed incompletely

        @app.get("/")
        def index_missing() -> JSONResponse:
            return JSONResponse(
                {"error": "dashboard assets are missing"}, status_code=500
            )

    return app


def run(config_path: str | None = None) -> None:
    """Entry point used by ``run.py`` and ``python -m poa``."""
    import uvicorn

    config = load_config(config_path)
    setup_logging(
        level=str(config.get("logging.level", "INFO")),
        file=config.resolve_path("logging.file"),
    )
    host = str(config.get("server.host", "127.0.0.1"))
    port = int(config.get("server.port", 8765))

    log.info("=" * 68)
    log.info("Pocket Option Technical Analysis Assistant")
    log.info("Dashboard:  http://%s:%s", host, port)
    log.info("Source:     %s", config.get("capture.source"))
    log.info(
        "Asset:      %s | chart %s | trade duration %s",
        config.get("market.asset"),
        format_duration(int(config.get("market.chart_timeframe", 60))),
        format_duration(int(config.get("market.trade_duration", 180))),
    )
    log.info("-" * 68)
    log.info("Analysis and alerts only. This tool never places a trade.")
    log.info("=" * 68)

    if config.get("server.open_browser"):
        import threading
        import webbrowser

        threading.Timer(1.5, lambda: webbrowser.open(f"http://{host}:{port}")).start()

    uvicorn.run(create_app(config), host=host, port=port, log_level="warning")

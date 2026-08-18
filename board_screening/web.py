"""FastAPI 登录、工作台与运行接口。"""

from __future__ import annotations

import hmac
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from board_screening.auth import LoginRateLimiter
from board_screening.config import Settings
from board_screening.divergence_screening import run_divergence_screening
from board_screening.export import records_to_csv_bytes, write_latest_csv
from board_screening.jobs import RunAlreadyActive, RunCoordinator, ScheduledRunService
from board_screening.stock_market_data import CachedStockKlineProvider, StockKlineCache
from board_screening.stock_screening import (
    run_stock_divergence_screening,
    run_stock_equal_decline_screening,
)
from board_screening.scheduler import (
    build_scheduler,
    fetch_latest_trade_date,
    submit_startup_catchup,
)
from board_screening.storage import RunRepository
from board_screening.market_data import CachedKlineProvider, KlineCache
from board_screening.strategies import (
    STRATEGY_EQUAL_DECLINE,
    STRATEGY_MACD_DIVERGENCE,
    RUN_MODE_LABELS,
    SUPPORTED_RUN_MODES,
    UNIVERSE_BOARD,
    UNIVERSE_STOCK,
    validate_run_mode,
    validate_strategy,
    validate_universe,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=PROJECT_ROOT / "templates")
RUN_STATUS_LABELS = {
    "queued": "等待执行",
    "running": "执行中",
    "succeeded": "成功",
    "succeeded_with_warnings": "成功（部分数据暂缺）",
    "failed": "失败",
    "skipped": "已跳过",
}


def _create_default_coordinator(settings: Settings, repository: RunRepository) -> RunCoordinator:
    cache = KlineCache(settings.database_path)
    cache.initialize()
    kline_provider = CachedKlineProvider(cache)
    stock_cache = StockKlineCache(settings.database_path)
    stock_cache.initialize()
    stock_kline_provider = CachedStockKlineProvider(stock_cache)

    def execute_equal_decline():
        from board_pattern_screener import run_screening

        latest_trade_date = fetch_latest_trade_date()
        return run_screening(
            kline_provider=kline_provider,
            required_trade_date=latest_trade_date,
        )

    def execute_macd_divergence():
        return run_divergence_screening(kline_provider)

    def execute_stock_equal_decline():
        return run_stock_equal_decline_screening(
            stock_kline_provider,
            required_trade_date=fetch_latest_trade_date(),
        )

    def execute_stock_macd_divergence():
        return run_stock_divergence_screening(stock_kline_provider)

    divergence_output_file = settings.divergence_output_file or settings.output_file.with_name(
        "ths_board_macd_divergence_result.csv"
    )
    stock_output_file = settings.stock_output_file or settings.output_file.with_name(
        "ths_stock_screen_result.csv"
    )
    stock_divergence_output_file = (
        settings.stock_divergence_output_file
        or settings.output_file.with_name("ths_stock_macd_divergence_result.csv")
    )

    return RunCoordinator(
        repository,
        execute_equal_decline,
        lambda records: write_latest_csv(
            records,
            settings.output_file,
            STRATEGY_EQUAL_DECLINE,
        ),
        mode_screeners={
            (UNIVERSE_BOARD, STRATEGY_EQUAL_DECLINE): execute_equal_decline,
            (UNIVERSE_BOARD, STRATEGY_MACD_DIVERGENCE): execute_macd_divergence,
            (UNIVERSE_STOCK, STRATEGY_EQUAL_DECLINE): execute_stock_equal_decline,
            (UNIVERSE_STOCK, STRATEGY_MACD_DIVERGENCE): execute_stock_macd_divergence,
        },
        mode_csv_writers={
            (UNIVERSE_BOARD, STRATEGY_EQUAL_DECLINE): lambda records: write_latest_csv(
                records,
                settings.output_file,
                STRATEGY_EQUAL_DECLINE,
            ),
            (UNIVERSE_BOARD, STRATEGY_MACD_DIVERGENCE): lambda records: write_latest_csv(
                records,
                divergence_output_file,
                STRATEGY_MACD_DIVERGENCE,
            ),
            (UNIVERSE_STOCK, STRATEGY_EQUAL_DECLINE): lambda records: write_latest_csv(
                records,
                stock_output_file,
                STRATEGY_EQUAL_DECLINE,
                UNIVERSE_STOCK,
            ),
            (UNIVERSE_STOCK, STRATEGY_MACD_DIVERGENCE): lambda records: write_latest_csv(
                records,
                stock_divergence_output_file,
                STRATEGY_MACD_DIVERGENCE,
                UNIVERSE_STOCK,
            ),
        },
    )


def _require_authenticated(request: Request, api: bool = False) -> Response | None:
    if request.session.get("authenticated"):
        return None
    if api:
        raise HTTPException(status_code=401, detail="请先登录")
    return RedirectResponse("/login", status_code=303)


def _validate_csrf(request: Request) -> None:
    expected_token = request.session.get("csrf_token", "")
    actual_token = request.headers.get("X-CSRF-Token", "")
    if not expected_token or not _secure_compare(expected_token, actual_token):
        raise HTTPException(status_code=403, detail="CSRF 校验失败")


def _secure_compare(left_value: str, right_value: str) -> bool:
    return hmac.compare_digest(left_value.encode("utf-8"), right_value.encode("utf-8"))


def create_app(
    settings: Settings | None = None,
    repository: RunRepository | None = None,
    coordinator: Any | None = None,
) -> FastAPI:
    app_settings = settings or Settings.from_env()
    app_repository = repository or RunRepository(app_settings.database_path)
    app_repository.initialize()
    KlineCache(app_settings.database_path).initialize()
    StockKlineCache(app_settings.database_path).initialize()
    app_coordinator = coordinator or _create_default_coordinator(app_settings, app_repository)
    login_limiter = LoginRateLimiter()
    scheduler = None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal scheduler
        if app_settings.enable_scheduler:
            scheduled_service = ScheduledRunService(
                app_repository,
                app_coordinator,
                fetch_latest_trade_date,
                modes=SUPPORTED_RUN_MODES,
            )
            scheduler = build_scheduler(scheduled_service)
            scheduler.start()
            try:
                latest_trade_date = fetch_latest_trade_date()
                submit_startup_catchup(scheduled_service, latest_trade_date)
            except Exception as exc:
                # 启动时交易日历不可用不应阻止网站提供上一版结果。
                logging.warning("启动补跑检查失败，网站将继续提供已有结果，原因：%s", exc)
        yield
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        app_coordinator.shutdown()

    app = FastAPI(title="板块形态监测", lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=app_settings.session_secret,
        same_site="strict",
        https_only=app_settings.cookie_secure,
        max_age=12 * 60 * 60,
    )
    static_directory = PROJECT_ROOT / "static"
    static_directory.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=static_directory), name="static")
    app.state.settings = app_settings
    app.state.repository = app_repository
    app.state.coordinator = app_coordinator

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login", response_class=HTMLResponse)
    def login(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
    ) -> Response:
        client_key = request.client.host if request.client else "unknown"
        if login_limiter.is_blocked(client_key):
            return templates.TemplateResponse(
                request,
                "login.html",
                {"error": "登录失败次数过多，请稍后再试"},
                status_code=429,
                headers={"Retry-After": "300"},
            )
        username_matches = _secure_compare(username, app_settings.username)
        password_matches = _secure_compare(password, app_settings.password)
        if not (username_matches and password_matches):
            login_limiter.record_failure(client_key)
            return templates.TemplateResponse(
                request,
                "login.html",
                {"error": "用户名或密码错误"},
                status_code=401,
            )
        login_limiter.clear(client_key)
        request.session.clear()
        request.session["authenticated"] = True
        request.session["csrf_token"] = secrets.token_urlsafe(32)
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(request: Request) -> Response:
        redirect = _require_authenticated(request)
        if redirect:
            return redirect
        _validate_csrf(request)
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        run_id: int | None = None,
        strategy: str = STRATEGY_EQUAL_DECLINE,
        universe: str = UNIVERSE_BOARD,
    ) -> Response:
        redirect = _require_authenticated(request)
        if redirect:
            return redirect
        try:
            selected_universe, selected_strategy = validate_run_mode(strategy, universe)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        latest_run = app_repository.get_current_run(selected_strategy, selected_universe)
        if run_id:
            selected_run = app_repository.get_run(run_id)
            if selected_run is None:
                raise HTTPException(status_code=404, detail="运行记录不存在")
            selected_strategy = str(selected_run["strategy"])
            selected_universe = str(selected_run.get("universe", UNIVERSE_BOARD))
            latest_run = app_repository.get_current_run(selected_strategy, selected_universe)
            status_run = selected_run
        else:
            status_run = latest_run
            if latest_run and latest_run["status"] not in {"succeeded", "succeeded_with_warnings"}:
                selected_run = (
                    app_repository.get_latest_successful_run(
                        selected_strategy,
                        selected_universe,
                    )
                    or latest_run
                )
            else:
                selected_run = latest_run
        results = app_repository.get_results(int(selected_run["id"])) if selected_run else []
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "runs": app_repository.get_runs(
                    strategy=selected_strategy,
                    universe=selected_universe,
                ),
                "selected_run": selected_run,
                "status_run": status_run,
                "results": results,
                "active_run_id": app_coordinator.active_run_id,
                "active_strategy": getattr(app_coordinator, "active_strategy", None),
                "selected_strategy": selected_strategy,
                "selected_universe": selected_universe,
                "selected_mode_label": RUN_MODE_LABELS[(selected_universe, selected_strategy)],
                "run_mode_labels": RUN_MODE_LABELS,
                "is_stock": selected_universe == UNIVERSE_STOCK,
                "csrf_token": request.session["csrf_token"],
                "status_labels": RUN_STATUS_LABELS,
            },
        )

    @app.post("/api/runs", status_code=status.HTTP_202_ACCEPTED)
    def start_run(
        request: Request,
        payload: dict[str, str] | None = Body(default=None),
    ) -> dict[str, object]:
        _require_authenticated(request, api=True)
        _validate_csrf(request)
        strategy = (payload or {}).get("strategy", STRATEGY_EQUAL_DECLINE)
        universe = (payload or {}).get("universe", UNIVERSE_BOARD)
        try:
            validate_run_mode(strategy, universe)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            if strategy == STRATEGY_EQUAL_DECLINE and universe == UNIVERSE_BOARD:
                run_id = app_coordinator.submit("manual")
            elif universe == UNIVERSE_BOARD:
                run_id = app_coordinator.submit("manual", strategy)
            else:
                run_id = app_coordinator.submit("manual", strategy, universe)
        except RunAlreadyActive as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"run_id": run_id, "status": "queued"}

    @app.get("/api/runs/current")
    def current_run(
        request: Request,
        strategy: str | None = Query(default=None),
        universe: str = Query(default=UNIVERSE_BOARD),
    ) -> dict[str, object] | None:
        _require_authenticated(request, api=True)
        try:
            validate_universe(universe)
            if strategy is not None:
                validate_strategy(strategy)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return app_repository.get_current_run(strategy, universe)

    @app.get("/api/runs")
    def list_runs(
        request: Request,
        strategy: str | None = Query(default=None),
        universe: str = Query(default=UNIVERSE_BOARD),
    ) -> list[dict[str, object]]:
        _require_authenticated(request, api=True)
        try:
            validate_universe(universe)
            if strategy is not None:
                validate_strategy(strategy)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return app_repository.get_runs(strategy=strategy, universe=universe)

    @app.get("/api/runs/{run_id}")
    def run_detail(request: Request, run_id: int) -> dict[str, object]:
        _require_authenticated(request, api=True)
        run = app_repository.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="运行记录不存在")
        return run

    @app.get("/api/runs/{run_id}/results")
    def run_results(
        request: Request,
        run_id: int,
        board_type: str | None = Query(default=None),
        search: str | None = Query(default=None),
        timeframe: str | None = Query(default=None),
        category: str | None = Query(default=None),
    ) -> list[dict[str, object]]:
        _require_authenticated(request, api=True)
        if app_repository.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="运行记录不存在")
        records = app_repository.get_results(run_id)
        if board_type:
            records = [record for record in records if record.get("板块类型") == board_type]
        if search:
            lowered_search = search.lower()
            records = [
                record
                for record in records
                if lowered_search
                in f"{record.get('板块名称', '')} {record.get('股票代码', '')} {record.get('股票名称', '')}".lower()
            ]
        if timeframe:
            records = [record for record in records if record.get("周期") == timeframe]
        if category:
            records = [record for record in records if category in str(record.get("背离分类", ""))]
        return records

    @app.get("/api/runs/{run_id}/csv")
    def download_csv(request: Request, run_id: int) -> Response:
        _require_authenticated(request, api=True)
        if app_repository.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="运行记录不存在")
        run = app_repository.get_run(run_id)
        strategy = str(run["strategy"])
        universe = str(run.get("universe", UNIVERSE_BOARD))
        content = records_to_csv_bytes(app_repository.get_results(run_id), strategy, universe)
        return Response(
            content=content,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="market-screening-{universe}-{strategy}-{run_id}.csv"'
                )
            },
        )

    return app


def create_default_app() -> FastAPI:
    """从部署环境读取配置并创建生产应用。"""
    return create_app(Settings.from_env())

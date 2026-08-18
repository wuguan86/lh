from __future__ import annotations

import re

from fastapi.testclient import TestClient

from board_screening.config import Settings
from board_screening.jobs import RunAlreadyActive
from board_screening.storage import RunRepository
from board_screening.strategies import STRATEGY_MACD_DIVERGENCE, UNIVERSE_STOCK
from board_screening.web import create_app


class FakeCoordinator:
    def __init__(
        self,
        reject: bool = False,
        active_run_id: int | None = None,
        active_strategy: str | None = None,
        active_universe: str | None = None,
    ) -> None:
        self.reject = reject
        self.active_run_id = active_run_id
        self.active_strategy = active_strategy
        self.active_universe = active_universe
        self.submitted_strategy = None
        self.submitted_universe = None

    def submit(
        self,
        trigger_type: str,
        strategy: str = "equal_decline",
        universe: str = "board",
    ) -> int:
        if self.reject:
            raise RunAlreadyActive("已有筛选任务正在执行")
        assert trigger_type == "manual"
        self.submitted_strategy = strategy
        self.submitted_universe = universe
        return 42

    def shutdown(self) -> None:
        return None


def build_client(tmp_path, coordinator: FakeCoordinator | None = None) -> TestClient:
    repository = RunRepository(tmp_path / "screening.db")
    repository.initialize()
    settings = Settings(
        username="admin",
        password="secret-password",
        session_secret="test-session-secret-with-enough-length",
        database_path=tmp_path / "screening.db",
        output_file=tmp_path / "latest.csv",
        cookie_secure=False,
        enable_scheduler=False,
    )
    app = create_app(settings, repository, coordinator or FakeCoordinator())
    return TestClient(app)


def login(client: TestClient) -> str:
    response = client.post(
        "/login",
        data={"username": "admin", "password": "secret-password"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


def test_health_is_public_but_dashboard_requires_login(tmp_path) -> None:
    with build_client(tmp_path) as client:
        assert client.get("/health").json() == {"status": "ok"}
        response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_login_rejects_wrong_password_and_renders_dashboard(tmp_path) -> None:
    with build_client(tmp_path) as client:
        wrong = client.post("/login", data={"username": "admin", "password": "wrong"})
        assert wrong.status_code == 401

        csrf_token = login(client)
        dashboard = client.get("/")

    assert csrf_token
    assert "板块形态监测" in dashboard.text
    assert dashboard.text.index("当前价格") < dashboard.text.index("1:1 等距")
    assert dashboard.text.index("1:1 等距") < dashboard.text.index("1.272 扩展")
    assert dashboard.text.index("1.272 扩展") < dashboard.text.index("1.618 扩展")
    assert dashboard.text.index("1.618 扩展") < dashboard.text.index("目标偏离率")


def test_non_ascii_credentials_and_invalid_csrf_fail_cleanly(tmp_path) -> None:
    with build_client(tmp_path) as client:
        login_response = client.post("/login", data={"username": "管理员", "password": "错误密码"})
        csrf_token = login(client)
        csrf_response = client.post("/api/runs", headers={"X-CSRF-Token": "invalid-token"})

    assert login_response.status_code == 401
    assert csrf_response.status_code == 403


def test_login_rate_limit_blocks_repeated_failures(tmp_path) -> None:
    with build_client(tmp_path) as client:
        responses = [
            client.post("/login", data={"username": "admin", "password": f"wrong-{index}"})
            for index in range(6)
        ]

    assert [response.status_code for response in responses[:5]] == [401] * 5
    assert responses[5].status_code == 429

    with build_client(tmp_path) as fresh_client:
        for index in range(6):
            fresh_client.post("/login", data={"username": "admin", "password": f"bad-{index}"})
        valid_response = fresh_client.post(
            "/login",
            data={"username": "admin", "password": "secret-password"},
            follow_redirects=True,
        )
    assert valid_response.status_code == 429
    assert "登录失败次数过多" in valid_response.text


def test_manual_run_requires_csrf_and_returns_202(tmp_path) -> None:
    with build_client(tmp_path) as client:
        csrf_token = login(client)
        forbidden = client.post("/api/runs")
        accepted = client.post("/api/runs", headers={"X-CSRF-Token": csrf_token})

    assert forbidden.status_code == 403
    assert accepted.status_code == 202
    assert accepted.json() == {"run_id": 42, "status": "queued"}


def test_manual_run_returns_409_when_job_is_active(tmp_path) -> None:
    with build_client(tmp_path, FakeCoordinator(reject=True)) as client:
        csrf_token = login(client)
        response = client.post("/api/runs", headers={"X-CSRF-Token": csrf_token})

    assert response.status_code == 409
    assert response.json()["detail"] == "已有筛选任务正在执行"


def test_csv_download_has_bom_and_adjacent_price_headers(tmp_path) -> None:
    with build_client(tmp_path) as client:
        csrf_token = login(client)
        repository: RunRepository = client.app.state.repository
        run_id = repository.create_run("manual")
        repository.save_results(
            run_id,
            [
                {
                    "板块类型": "概念",
                    "板块名称": "5G",
                    "最新交易日": "2026-07-24",
                    "当前价格": 95.0,
                    "1:1等距目标价": 100.0,
                    "1.272扩展目标价": 86.4,
                    "1.618扩展目标价": 69.1,
                    "目标偏离率": "5.00%",
                }
            ],
        )
        repository.finish_run(run_id, "succeeded", "2026-07-24", 1, 0)

        response = client.get(f"/api/runs/{run_id}/csv", headers={"X-CSRF-Token": csrf_token})

    assert response.status_code == 200
    assert response.content.startswith(b"\xef\xbb\xbf")
    header = response.content.decode("utf-8-sig").splitlines()[0]
    assert "当前价格,1:1等距目标价,1.272扩展目标价,1.618扩展目标价,目标偏离率" in header


def test_dashboard_contains_history_filters_status_and_detail_surface(tmp_path) -> None:
    with build_client(tmp_path) as client:
        login(client)
        response = client.get("/")

    assert 'id="run-history"' in response.text
    assert 'id="board-type-filter"' in response.text
    assert 'id="board-search"' in response.text
    assert 'id="run-status"' in response.text
    assert 'id="result-detail"' in response.text
    assert "下载 CSV" in response.text


def test_dashboard_limits_history_and_displays_run_ids(tmp_path) -> None:
    with build_client(tmp_path) as client:
        login(client)
        repository: RunRepository = client.app.state.repository
        for _ in range(35):
            run_id = repository.create_run("manual")
            repository.finish_run(run_id, "failed", None, 0, 0, "测试失败")

        response = client.get("/")

    assert response.text.count("<option value=") == 30
    assert "#35 ·" in response.text
    assert "#5 ·" not in response.text


def test_dashboard_translates_internal_run_status_to_chinese(tmp_path) -> None:
    with build_client(tmp_path) as client:
        login(client)
        repository: RunRepository = client.app.state.repository
        run_id = repository.create_run("manual")
        repository.finish_run(run_id, "succeeded_with_warnings", "2026-07-24", 2, 1)

        response = client.get(f"/?run_id={run_id}")

    assert "成功（部分数据暂缺）" in response.text
    assert ">succeeded_with_warnings<" not in response.text


def test_dashboard_exposes_numeric_sort_controls(tmp_path) -> None:
    with build_client(tmp_path) as client:
        login(client)
        response = client.get("/")

    assert 'data-sort-key="currentPrice"' in response.text
    assert 'data-sort-key="targetPrice"' in response.text
    assert 'data-sort-key="targetDeviation"' in response.text
    assert 'data-sort-key="maxDrawdown"' in response.text


def test_dashboard_keeps_last_successful_results_after_latest_failure(tmp_path) -> None:
    with build_client(tmp_path) as client:
        login(client)
        repository: RunRepository = client.app.state.repository
        successful_run_id = repository.create_run("scheduled")
        repository.save_results(
            successful_run_id,
            [
                {
                    "板块类型": "概念",
                    "板块名称": "5G",
                    "最新交易日": "2026-07-24",
                    "当前价格": 95.0,
                    "1:1等距目标价": 100.0,
                    "1.272扩展目标价": 86.4,
                    "1.618扩展目标价": 69.1,
                    "目标偏离率": "5.00%",
                }
            ],
        )
        repository.finish_run(successful_run_id, "succeeded", "2026-07-24", 1, 0)
        failed_run_id = repository.create_run("scheduled")
        repository.finish_run(failed_run_id, "failed", None, 0, 0, "核心行情失败")

        response = client.get("/")

    assert "5G" in response.text
    assert "失败" in response.text
    assert f'/api/runs/{successful_run_id}/csv' in response.text


def test_dashboard_exposes_active_run_for_automatic_polling(tmp_path) -> None:
    coordinator = FakeCoordinator(
        active_run_id=77,
        active_strategy="equal_decline",
        active_universe="board",
    )
    with build_client(tmp_path, coordinator) as client:
        login(client)
        response = client.get("/")

    assert 'data-active-run-id="77"' in response.text


def test_dashboard_does_not_show_other_mode_as_active(tmp_path) -> None:
    coordinator = FakeCoordinator(
        active_run_id=88,
        active_strategy=STRATEGY_MACD_DIVERGENCE,
        active_universe=UNIVERSE_STOCK,
    )
    with build_client(tmp_path, coordinator) as client:
        login(client)
        response = client.get("/?strategy=equal_decline&universe=stock")

    assert 'data-active-run-id=""' in response.text
    assert '<span class="run-label">立即执行</span>' in response.text
    assert '<span class="run-label">执行中</span>' not in response.text


def test_failed_run_shows_reason_and_disables_empty_csv(tmp_path) -> None:
    with build_client(tmp_path) as client:
        login(client)
        repository: RunRepository = client.app.state.repository
        run_id = repository.create_run("manual", universe=UNIVERSE_STOCK)
        repository.finish_run(run_id, "failed", None, 0, 0, "市值快照连接失败")

        response = client.get(f"/?run_id={run_id}")

    assert "运行失败" in response.text
    assert "最近一次运行失败" in response.text
    assert "市值快照连接失败" in response.text
    assert 'id="download-csv"' not in response.text
    assert 'aria-disabled="true"' in response.text
    assert '<span class="run-label">立即执行</span>' in response.text


def test_run_detail_api_and_unknown_result_run_contract(tmp_path) -> None:
    with build_client(tmp_path) as client:
        login(client)
        repository: RunRepository = client.app.state.repository
        run_id = repository.create_run("manual")

        detail_response = client.get(f"/api/runs/{run_id}")
        missing_results_response = client.get("/api/runs/999/results")

    assert detail_response.status_code == 200
    assert detail_response.json()["id"] == run_id
    assert missing_results_response.status_code == 404


def test_divergence_dashboard_exposes_strategy_period_and_category_filters(tmp_path) -> None:
    with build_client(tmp_path) as client:
        login(client)
        response = client.get(f"/?strategy={STRATEGY_MACD_DIVERGENCE}")

    assert "MACD底背离筛选结果" in response.text
    assert 'id="timeframe-filter"' in response.text
    assert 'id="category-filter"' in response.text
    assert "线和绿柱双背离" in response.text


def test_manual_run_accepts_divergence_strategy(tmp_path) -> None:
    coordinator = FakeCoordinator()
    with build_client(tmp_path, coordinator) as client:
        csrf_token = login(client)
        response = client.post(
            "/api/runs",
            headers={"X-CSRF-Token": csrf_token},
            json={"strategy": STRATEGY_MACD_DIVERGENCE},
        )

    assert response.status_code == 202
    assert coordinator.submitted_strategy == STRATEGY_MACD_DIVERGENCE


def test_divergence_csv_uses_diagnostic_columns(tmp_path) -> None:
    with build_client(tmp_path) as client:
        login(client)
        repository: RunRepository = client.app.state.repository
        run_id = repository.create_run("manual", strategy=STRATEGY_MACD_DIVERGENCE)
        repository.save_results(
            run_id,
            [
                {
                    "筛选策略": "MACD底背离",
                    "板块类型": "概念",
                    "板块名称": "5G",
                    "周期": "日线",
                    "背离分类": "单纯底背离",
                    "背离次数": 1,
                    "最新交易日": "2026-07-24",
                    "当前价格": 95.0,
                }
            ],
        )
        response = client.get(f"/api/runs/{run_id}/csv")

    header = response.content.decode("utf-8-sig").splitlines()[0]
    assert "筛选策略,板块类型,板块名称,周期,背离分类,背离次数" in header
    assert "1:1等距目标价" not in header


def test_dashboard_exposes_two_independent_stock_entries(tmp_path) -> None:
    with build_client(tmp_path) as client:
        login(client)
        response = client.get("/?strategy=equal_decline&universe=stock")

    assert "个股等距下跌" in response.text
    assert "个股MACD底背离" in response.text
    assert "沪深 A 股 / 总市值 &gt; 300 亿元" in response.text
    assert "股票名称或代码" in response.text
    assert 'id="board-type-filter"' not in response.text
    assert 'data-sort-key="marketCap"' in response.text


def test_manual_run_accepts_stock_universe(tmp_path) -> None:
    coordinator = FakeCoordinator()
    with build_client(tmp_path, coordinator) as client:
        csrf_token = login(client)
        response = client.post(
            "/api/runs",
            headers={"X-CSRF-Token": csrf_token},
            json={"strategy": "equal_decline", "universe": UNIVERSE_STOCK},
        )

    assert response.status_code == 202
    assert coordinator.submitted_strategy == "equal_decline"
    assert coordinator.submitted_universe == UNIVERSE_STOCK


def test_stock_csv_uses_stock_identity_and_market_cap(tmp_path) -> None:
    with build_client(tmp_path) as client:
        login(client)
        repository: RunRepository = client.app.state.repository
        run_id = repository.create_run("manual", universe=UNIVERSE_STOCK)
        repository.save_results(
            run_id,
            [
                {
                    "股票代码": "600001",
                    "股票名称": "测试股票",
                    "总市值（亿元）": 501.0,
                    "最新交易日": "2026-07-24",
                    "当前价格": 95.0,
                    "1:1等距目标价": 100.0,
                    "目标偏离率": "5.00%",
                }
            ],
        )
        response = client.get(f"/api/runs/{run_id}/csv")

    header = response.content.decode("utf-8-sig").splitlines()[0]
    assert header.startswith("股票代码,股票名称,总市值（亿元）")
    assert "板块类型" not in header
    assert "关联ETF代码" not in header

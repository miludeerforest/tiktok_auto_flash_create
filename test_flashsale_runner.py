import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast

import captcha_solver
import flashsale_runner as runner


def test_load_seed_schedule_reads_gui_config_only(tmp_path):
    runner.configure_paths(str(tmp_path))
    cfg_path = tmp_path / "gui_config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "seed_names": [
                    "引流-2026-3.3-00:30",
                    "微利-2026-3.3-01:00-abcd",
                    "盈利-2026-3.3-01:30",
                    "平本-2026-3.3-02:00",
                    "",
                    "平本-2026-3.3-02:00",
                    "bad-format",
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    schedule = runner.load_seed_schedule()

    assert [item["name"] for item in schedule] == [
        "引流-2026-3.3-00:30",
        "微利-2026-3.3-01:00",
        "盈利-2026-3.3-01:30",
        "平本-2026-3.3-02:00",
    ]
    assert [item["dt"] for item in schedule] == [
        datetime(2026, 3, 3, 0, 30),
        datetime(2026, 3, 3, 1, 0),
        datetime(2026, 3, 3, 1, 30),
        datetime(2026, 3, 3, 2, 0),
    ]


def test_choose_from_seed_schedule_prefers_same_prefix_source(tmp_path):
    runner.configure_paths(str(tmp_path))
    (tmp_path / "gui_config.json").write_text(
        json.dumps(
            {
                "seed_names": [
                    "引流-2026-3.3-00:30",
                    "微利-2026-3.3-01:00",
                    "引流-2026-3.3-01:30",
                    "平本-2026-3.3-02:00",
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    upcoming_rows: list[runner.RawUpcomingRow] = [
        {"name": "引流-2026-3.3-00:30", "text": "引流-2026-3.3-00:30 Upcoming", "hasDuplicate": True},
        {"name": "微利-2026-3.3-01:00", "text": "微利-2026-3.3-01:00 Upcoming", "hasDuplicate": True},
    ]

    decision = runner.choose_from_seed_schedule(runner.filter_usable_upcoming_rows(upcoming_rows))
    plan = decision.get("plan")

    assert decision.get("status") == "planned"
    assert plan is not None
    assert plan["new_name"] == "引流-2026-3.3-01:30"
    assert plan["source_name"] == "引流-2026-3.3-00:30"
    assert plan["source_prefix"] == "引流"


def test_assess_create_page_product_state_blocks_empty_products():
    result = runner.assess_create_page_product_state(
        {
            "bodyText": "Promotion products No products found",
            "rowTexts": ["Product Price Stock"],
            "blockTexts": [],
            "emptyTexts": ["No products found"],
            "rowCount": 0,
            "blockCount": 0,
        }
    )

    assert result["ok"] is False
    assert result["reason"] == "empty_products"


def test_assess_create_page_product_state_accepts_visible_products():
    result = runner.assess_create_page_product_state(
        {
            "bodyText": "Promotion products Product A Price 99 Stock 10",
            "rowTexts": ["Product A Price 99 Stock 10"],
            "blockTexts": [],
            "emptyTexts": [],
            "rowCount": 1,
            "blockCount": 0,
        }
    )

    assert result["ok"] is True
    assert result["reason"] == "products_confirmed"
    assert result["visible_product_count"] >= 1


def test_build_drag_distance_candidates_prefers_unique_positive_values():
    distances = captcha_solver.build_drag_distance_candidates(
        gap_left_px=186,
        image_width_px=300,
        background_width_css=280,
        slider_width_css=42,
        track_width_css=260,
    )

    assert distances
    assert all(distance > 0 for distance in distances)
    assert len(distances) == len(set(int(round(item)) for item in distances))


def test_select_best_gap_candidate_prefers_piece_aware_high_confidence():
    candidates = [
        captcha_solver.GapCandidate("variance", 170.0, 0.60, False, ""),
        captcha_solver.GapCandidate("template", 168.0, 0.58, True, ""),
        captcha_solver.GapCandidate("contour", 175.0, 0.65, False, ""),
    ]

    best = captcha_solver.select_best_gap_candidate(candidates, image_width_px=320)

    assert best is not None
    assert best.strategy == "template"


def test_select_gap_candidates_prefers_piece_consensus_first():
    candidates = [
        captcha_solver.GapCandidate("template", 168.0, 0.53, True, ""),
        captcha_solver.GapCandidate("sobel", 172.0, 0.51, True, ""),
        captcha_solver.GapCandidate("yolo", 190.0, 0.91, False, ""),
    ]

    selected = captcha_solver.select_gap_candidates(candidates, image_width_px=320, limit=2)

    assert len(selected) == 2
    assert selected[0].strategy.startswith("consensus:")
    assert selected[1].strategy == "template"


def test_select_gap_candidates_prefers_piece_tier_before_yolo():
    candidates = [
        captcha_solver.GapCandidate("template", 165.0, 0.44, True, ""),
        captcha_solver.GapCandidate("yolo", 166.0, 0.95, False, ""),
        captcha_solver.GapCandidate("variance", 167.0, 0.99, False, ""),
    ]

    selected = captcha_solver.select_gap_candidates(candidates, image_width_px=320, limit=2)

    assert len(selected) == 2
    assert selected[0].strategy == "template"
    assert selected[1].strategy == "yolo"


def test_scene_geometry_requires_background_to_cover_slider_lane():
    background = captcha_solver.ElementSnapshot(
        locator=cast(Any, SimpleNamespace()),
        box={"x": 10.0, "y": 10.0, "width": 120.0, "height": 60.0},
        tag_name="IMG",
        class_name="captcha-bg",
    )
    slider = captcha_solver.ElementSnapshot(
        locator=cast(Any, SimpleNamespace()),
        box={"x": 200.0, "y": 120.0, "width": 42.0, "height": 42.0},
        tag_name="DIV",
        class_name="slider-handle",
    )

    assert captcha_solver._scene_geometry_is_plausible(background, slider, None) is False


def test_try_auto_solve_captcha_uses_context_pages(monkeypatch):
    target_page = object()
    solved_page = object()
    context = SimpleNamespace(pages=[target_page, solved_page])

    class FakeOutcome:
        def __init__(self, solved: bool):
            self.solved = solved
            self.reason = "provisional_success" if solved else "failed"

    async def fake_solver(page):
        return FakeOutcome(page is solved_page)

    async def fake_detect(page):
        return page is solved_page

    monkeypatch.setattr(runner, "has_captcha_solver", True)
    monkeypatch.setattr(runner, "solve_slider_captcha_with_result", fake_solver)
    monkeypatch.setattr(runner, "detect_slider_captcha", fake_detect)
    monkeypatch.setattr(runner, "auto_solve_captcha_enabled", True)

    result = runner.asyncio.run(runner.try_auto_solve_captcha(target_page, context))

    assert result is True


def test_try_auto_solve_captcha_respects_runtime_toggle(monkeypatch):
    async def fake_solver(_page):
        raise AssertionError("solver should not run when auto captcha is disabled")

    monkeypatch.setattr(runner, "has_captcha_solver", True)
    monkeypatch.setattr(runner, "solve_slider_captcha_with_result", fake_solver)
    monkeypatch.setattr(runner, "auto_solve_captcha_enabled", False)

    result = runner.asyncio.run(runner.try_auto_solve_captcha(object(), None))

    assert result is False

import json
from datetime import datetime

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

"""Rule tests for the sorting (waste-category) uploads and the category table."""

import pytest

from warehouse import service
from warehouse.errors import BatchRejected, ConflictError, ValidationError


def wb_bytes(rows):
    text = "单号,清运月,街道,净重（千克）,修订号\n"
    text += "".join(",".join(str(p) for p in r) + "\n" for r in rows)
    return text.encode("utf-8")


def sort_bytes(rows, header="单号,修订号,类别,克重"):
    text = header + "\n"
    text += "".join(",".join(str(p) for p in r) + "\n" for r in rows)
    return text.encode("utf-8")


def upload_wb(db_path, rows):
    return service.upload_batch(db_path, "wb.csv", wb_bytes(rows))


def upload_sort(db_path, rows, header="单号,修订号,类别,克重"):
    return service.upload_sorting(db_path, "sorting.csv", sort_bytes(rows, header))


def categories_for(report, month):
    view = next(item for item in report["months"] if item["month"] == month)
    return {row["category"]: row for row in view["categories"]}, view


def test_sorting_requires_booked_ticket(db_path):
    service.initialize(db_path, "2026-10")
    with pytest.raises(BatchRejected) as exc:
        upload_sort(db_path, [("A1", 1, "厨余垃圾", 1000)])
    assert "尚无地磅入账" in exc.value.details[0]["message"]


def test_sorting_revision_must_be_booked(initialized):
    db_path = initialized
    upload_wb(db_path, [("A1", "2026-10", "城东", 100, 1)])
    # A revision above the current one does not exist.
    with pytest.raises(BatchRejected) as exc:
        upload_sort(db_path, [("A1", 2, "厨余垃圾", 100000)])
    assert "尚未入账" in exc.value.details[0]["message"]


def test_sorting_sum_must_equal_booked_net(initialized):
    db_path = initialized
    upload_wb(db_path, [("A1", "2026-10", "城东", 100, 1)])
    with pytest.raises(BatchRejected) as exc:
        upload_sort(
            db_path,
            [("A1", 1, "厨余垃圾", 60000), ("A1", 1, "其他垃圾", 30000)],
        )
    message = exc.value.details[0]["message"]
    assert "90000" in message and "100000" in message


def test_zero_weight_split_is_allowed(initialized):
    db_path = initialized
    upload_wb(db_path, [("A1", "2026-10", "城东", 0, 1)])
    result = upload_sort(db_path, [("A1", 1, "厨余垃圾", 0)])
    assert result["summary"]["tickets"] == 1


def test_zero_weight_booking_without_sorting_stays_out_of_pending(initialized):
    db_path = initialized
    upload_wb(db_path, [("A1", "2026-10", "城东", 0, 1)])
    report = service.classification_view(db_path, "2026-10", "2026-10")
    assert report["pending_count"] == 0
    cats, _ = categories_for(report, "2026-10")
    # No weight at all: nothing to categorise, so no uncategorized row.
    assert cats == {}


def test_negative_and_fractional_grams_rejected_at_parse(initialized):
    db_path = initialized
    upload_wb(db_path, [("A1", "2026-10", "城东", 100, 1)])
    with pytest.raises(ValidationError):
        upload_sort(db_path, [("A1", 1, "厨余垃圾", -1)])
    with pytest.raises(ValidationError):
        upload_sort(db_path, [("A1", 1, "厨余垃圾", 12.5)])


def test_reserved_category_name_rejected(initialized):
    db_path = initialized
    upload_wb(db_path, [("A1", "2026-10", "城东", 100, 1)])
    with pytest.raises(ValidationError) as exc:
        upload_sort(db_path, [("A1", 1, "未分类", 100000)])
    assert any("未分类" in item["message"] for item in exc.value.details)


def test_duplicate_category_row_in_batch_rejected(initialized):
    db_path = initialized
    upload_wb(db_path, [("A1", "2026-10", "城东", 100, 1)])
    with pytest.raises(BatchRejected) as exc:
        upload_sort(
            db_path,
            [
                ("A1", 1, "厨余垃圾", 60000),
                ("A1", 1, "厨余垃圾", 40000),
            ],
        )
    assert "重复" in exc.value.details[0]["message"]


def test_failed_exchange_keeps_original_categories(initialized):
    db_path = initialized
    upload_wb(db_path, [("A1", "2026-10", "城东", 100, 1)])
    upload_sort(db_path, [("A1", 1, "厨余垃圾", 100000)])

    with pytest.raises(BatchRejected):
        upload_sort(
            db_path,
            [
                ("A1", 1, "其他垃圾", 90000),  # sums to 90k -> reject
            ],
        )
    report = service.classification_view(db_path, "2026-10", "2026-10")
    cats, view = categories_for(report, "2026-10")
    assert set(cats) == {"厨余垃圾"}
    assert cats["厨余垃圾"]["current_grams"] == 100000
    assert view["matches_bottom"]


def test_uncategorized_weight_and_pending_list(initialized):
    db_path = initialized
    upload_wb(db_path, [("A1", "2026-10", "城东", 100, 1)])
    report = service.classification_view(db_path, "2026-10", "2026-10")
    cats, view = categories_for(report, "2026-10")
    assert cats["未分类"]["current_grams"] == 100000
    assert cats["未分类"].get("reserved") is True
    assert view["matches_bottom"]
    assert report["pending"] == [
        {
            "ticket_no": "A1",
            "revision": 1,
            "net_grams": 100000,
            "collection_month": "2026-10",
        }
    ]

    # Supplying the split clears the pending entry.
    upload_sort(db_path, [("A1", 1, "厨余垃圾", 100000)])
    report = service.classification_view(db_path, "2026-10", "2026-10")
    assert report["pending_count"] == 0
    cats, _ = categories_for(report, "2026-10")
    assert "未分类" not in cats


def test_two_monthly_columns_both_match_bottom_table(initialized):
    db_path = initialized
    upload_wb(db_path, [("A1", "2026-10", "城东", 100, 1)])
    service.seal_month(db_path, "2026-10")
    # Late ticket (old month) lands in November as adjustment.
    upload_wb(db_path, [("A2", "2026-09", "城东", 30, 1)])
    upload_sort(
        db_path,
        [
            ("A1", 1, "厨余垃圾", 100000),
            ("A2", 1, "其他垃圾", 30000),
        ],
    )
    report = service.classification_view(db_path, "2026-10", "2026-11")
    for view in report["months"]:
        assert view["matches_bottom"], view
        bottom = service.month_view(db_path, view["month"])["table"]["totals"]
        assert view["totals"]["current_grams"] == bottom["current_grams"]
        assert view["totals"]["adjustment_grams"] == bottom["adjustment_grams"]
    nov, _ = categories_for(report, "2026-11")
    assert nov["其他垃圾"]["adjustment_grams"] == 30000


def test_reversal_follows_reversed_bookings_categories(initialized):
    db_path = initialized
    upload_wb(db_path, [("A1", "2026-10", "城东", 100, 1)])
    service.seal_month(db_path, "2026-10")
    upload_wb(db_path, [("A1", "2026-10", "城西", 120, 2)])
    upload_sort(
        db_path,
        [
            ("A1", 1, "厨余垃圾", 60000),
            ("A1", 1, "其他垃圾", 40000),
            ("A1", 2, "厨余垃圾", 70000),
            ("A1", 2, "可回收物", 50000),
        ],
    )
    report = service.classification_view(db_path, "2026-11", "2026-11")
    cats, view = categories_for(report, "2026-11")
    # Reversal of rev1 (100kg) follows rev1's split; new booking follows rev2.
    assert cats["厨余垃圾"]["adjustment_grams"] == -60000 + 70000
    assert cats["其他垃圾"]["adjustment_grams"] == -40000
    assert cats["可回收物"]["adjustment_grams"] == 50000
    assert view["totals"]["adjustment_grams"] == 20000
    assert view["matches_bottom"]


def test_reversal_outside_query_range_still_deducted_by_category(initialized):
    db_path = initialized
    upload_wb(db_path, [("A1", "2026-09", "城东", 100, 1)])
    service.seal_month(db_path, "2026-10")
    # Correction booked into November; original September booking is outside
    # an October–November query's split-visible bookings but still resolvable.
    upload_wb(db_path, [("A1", "2026-09", "城东", 80, 2)])
    upload_sort(
        db_path,
        [
            ("A1", 1, "厨余垃圾", 100000),
            ("A1", 2, "其他垃圾", 80000),
        ],
    )
    report = service.classification_view(db_path, "2026-11", "2026-11")
    cats, view = categories_for(report, "2026-11")
    assert cats["厨余垃圾"]["adjustment_grams"] == -100000
    assert cats["其他垃圾"]["adjustment_grams"] == 80000
    assert view["matches_bottom"]
    assert report["pending_count"] == 0


def test_noop_revision_bump_falls_back_to_last_booked_split(initialized):
    db_path = initialized
    upload_wb(db_path, [("A1", "2026-10", "城东", 100, 1)])
    service.seal_month(db_path, "2026-10")
    upload_wb(db_path, [("A1", "2026-10", "城西", 120, 2)])
    # Revision 3 only advances the number; no booking, no sorting split.
    upload_wb(db_path, [("A1", "2026-10", "城西", 120, 3)])
    service.seal_month(db_path, "2026-11")
    # A later correction's reversal points at rev3 and must use rev2's split.
    upload_wb(db_path, [("A1", "2026-10", "城东", 140, 4)])
    upload_sort(
        db_path,
        [
            ("A1", 2, "厨余垃圾", 70000),
            ("A1", 2, "可回收物", 50000),
            ("A1", 4, "厨余垃圾", 140000),
        ],
    )
    report = service.classification_view(db_path, "2026-12", "2026-12")
    cats, view = categories_for(report, "2026-12")
    assert cats["厨余垃圾"]["adjustment_grams"] == -70000 + 140000
    assert cats["可回收物"]["adjustment_grams"] == -50000
    assert view["matches_bottom"]
    assert report["pending_count"] == 0


def test_noop_revision_cannot_receive_sorting(initialized):
    db_path = initialized
    upload_wb(db_path, [("A1", "2026-10", "城东", 100, 1)])
    upload_wb(db_path, [("A1", "2026-10", "城东", 100, 2)])  # no-op bump
    with pytest.raises(BatchRejected) as exc:
        upload_sort(db_path, [("A1", 2, "厨余垃圾", 100000)])
    assert "没有对应入账" in exc.value.details[0]["message"]


def test_late_sorting_after_seal_recategorizes_original_and_reversal(initialized):
    db_path = initialized
    upload_wb(db_path, [("A1", "2026-10", "城东", 100, 1)])
    service.seal_month(db_path, "2026-10")
    upload_wb(db_path, [("A1", "2026-10", "城西", 120, 2)])
    # Sealed snapshot must remain downloadable and byte-stable.
    before = service.snapshot_csv(db_path, "2026-10")

    # Sorting data arrives after both months moved on.
    upload_sort(
        db_path,
        [
            ("A1", 1, "厨余垃圾", 100000),
            ("A1", 2, "其他垃圾", 120000),
        ],
    )
    after = service.snapshot_csv(db_path, "2026-10")
    assert before == after

    report = service.classification_view(db_path, "2026-10", "2026-11")
    oct_cats, oct_view = categories_for(report, "2026-10")
    nov_cats, nov_view = categories_for(report, "2026-11")
    assert oct_cats["厨余垃圾"]["current_grams"] == 100000
    assert nov_cats["厨余垃圾"]["adjustment_grams"] == -100000
    assert nov_cats["其他垃圾"]["adjustment_grams"] == 120000
    assert oct_view["matches_bottom"] and nov_view["matches_bottom"]


def test_reexchange_replaces_only_listed_tickets(initialized):
    db_path = initialized
    upload_wb(db_path, [("A1", "2026-10", "城东", 100, 1)])
    upload_wb(db_path, [("A2", "2026-10", "城东", 50, 1)])
    upload_sort(
        db_path,
        [
            ("A1", 1, "厨余垃圾", 100000),
            ("A2", 1, "厨余垃圾", 50000),
        ],
    )
    # Re-exchange only A1; A2 keeps its old category.
    upload_sort(db_path, [("A1", 1, "其他垃圾", 100000)])
    report = service.classification_view(db_path, "2026-10", "2026-10")
    cats, view = categories_for(report, "2026-10")
    assert cats["其他垃圾"]["current_grams"] == 100000
    assert cats["厨余垃圾"]["current_grams"] == 50000
    assert view["matches_bottom"]


def test_report_range_and_future_end_rejected(initialized):
    db_path = initialized
    upload_wb(db_path, [("A1", "2026-10", "城东", 100, 1)])
    with pytest.raises(ConflictError):
        service.classification_view(db_path, "2026-10", "2026-12")
    with pytest.raises(ValidationError):
        service.classification_view(db_path, "2026-11", "2026-10")


def test_report_lists_every_month_in_range(initialized):
    db_path = initialized
    upload_wb(db_path, [("A1", "2026-10", "城东", 100, 1)])
    service.seal_month(db_path, "2026-10")
    report = service.classification_view(db_path, "2026-10", "2026-11")
    assert [item["month"] for item in report["months"]] == [
        "2026-10",
        "2026-11",
    ]
    empty = report["months"][1]
    assert empty["categories"] == []
    assert empty["matches_bottom"]


def test_multiple_tickets_aggregate_into_same_category(initialized):
    db_path = initialized
    upload_wb(db_path, [("A1", "2026-10", "城东", 100, 1)])
    upload_wb(db_path, [("A2", "2026-10", "城西", 40, 1)])
    upload_sort(
        db_path,
        [
            ("A1", 1, "厨余垃圾", 100000),
            ("A2", 1, "厨余垃圾", 40000),
        ],
    )
    report = service.classification_view(db_path, "2026-10", "2026-10")
    cats, view = categories_for(report, "2026-10")
    assert cats["厨余垃圾"]["current_grams"] == 140000
    assert view["totals"]["total_grams"] == 140000


def test_sorting_header_aliases_and_encoding(initialized):
    db_path = initialized
    upload_wb(db_path, [("A1", "2026-10", "城东", 100, 1)])
    data = "单号,版本号,垃圾类别,重量克\nA1,1,厨余垃圾,100000\n".encode("gb18030")
    result = service.upload_sorting(db_path, "gbk.csv", data)
    assert result["summary"]["rows"] == 1
    report = service.classification_view(db_path, "2026-10", "2026-10")
    cats, _ = categories_for(report, "2026-10")
    assert "厨余垃圾" in cats

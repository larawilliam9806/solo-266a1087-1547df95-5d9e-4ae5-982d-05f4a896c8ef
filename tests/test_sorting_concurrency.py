"""Snapshot and concurrency tests for the category monthly table.

The report must be computed from one consistent snapshot even while tickets
keep arriving, months seal and sorting batches exchange.  Sorting exchanges
are themselves write transactions, so they serialise against everything else.
"""

import sqlite3
import threading

from warehouse import service
from warehouse.errors import BatchRejected


def wb_bytes(rows):
    text = "单号,清运月,街道,净重（千克）,修订号\n"
    text += "".join(",".join(str(p) for p in r) + "\n" for r in rows)
    return text.encode("utf-8")


def sort_bytes(rows):
    text = "单号,修订号,类别,克重\n"
    text += "".join(",".join(str(p) for p in r) + "\n" for r in rows)
    return text.encode("utf-8")


def test_report_uses_one_snapshot_amid_concurrent_commits(db_path, monkeypatch):
    service.initialize(db_path, "2026-10")
    service.upload_batch(db_path, "wb.csv", wb_bytes([("A1", "2026-10", "城东", 100, 1)]))

    committed = threading.Event()

    import warehouse.storage as storage_mod

    real_connect = storage_mod.connect

    class _ConnProxy:
        def __init__(self, conn):
            object.__setattr__(self, "_conn", conn)

        def execute(self, sql, *args, **kwargs):
            result = self._conn.execute(sql, *args, **kwargs)
            if (
                not getattr(patched_connect, "_held", False)
                and "FROM bookings" in sql
                and "target_month BETWEEN" in sql
            ):
                patched_connect._held = True
                # The first report SELECT established the WAL snapshot.
                # Park so the concurrent writer commits after the snapshot is
                # taken but before the report reads the sorting data.
                committed.wait(timeout=10)
            return result

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def __setattr__(self, name, value):
            setattr(self._conn, name, value)

    def patched_connect(path):
        conn = real_connect(path)
        if not getattr(patched_connect, "_armed", False):
            patched_connect._armed = True
            return _ConnProxy(conn)
        return conn

    monkeypatch.setattr(storage_mod, "connect", patched_connect)

    result = {}

    def run_report():
        result["report"] = service.classification_view(db_path, "2026-10", "2026-10")

    thread = threading.Thread(target=run_report)
    thread.start()

    # Give the report thread time to open its snapshot.
    while not getattr(patched_connect, "_held", False):
        pass
    # Concurrent commit: a new ticket and its sorting data land while the
    # report transaction is open.
    service.upload_batch(
        db_path, "wb2.csv", wb_bytes([("A2", "2026-10", "城西", 50, 1)])
    )
    service.upload_sorting(
        db_path, "s.csv", sort_bytes([("A2", 1, "厨余垃圾", 50000)])
    )
    committed.set()
    thread.join(timeout=15)

    report = result["report"]
    month = report["months"][0]
    # The snapshot predates A2: it must not appear, and the bottom cross-check
    # computed from that same snapshot still holds exactly.
    assert month["matches_bottom"]
    categories = {row["category"]: row for row in month["categories"]}
    assert "厨余垃圾" not in categories
    assert categories["未分类"]["current_grams"] == 100000
    assert {(p["ticket_no"], p["revision"]) for p in report["pending"]} == {
        ("A1", 1)
    }

    # A fresh report sees the committed data.
    fresh = service.classification_view(db_path, "2026-10", "2026-10")
    fresh_cats = {row["category"]: row for row in fresh["months"][0]["categories"]}
    assert fresh_cats["厨余垃圾"]["current_grams"] == 50000
    assert fresh["months"][0]["matches_bottom"]


def test_concurrent_sorting_batches_serialise_without_losing_rows(db_path):
    service.initialize(db_path, "2026-10")
    tickets = []
    for index in range(6):
        ticket = f"T{index}"
        tickets.append(ticket)
        service.upload_batch(
            db_path,
            f"wb{index}.csv",
            wb_bytes([(ticket, "2026-10", "城东", 10 + index, 1)]),
        )

    barrier = threading.Barrier(6)
    errors = []
    lock = threading.Lock()

    def worker(index):
        try:
            barrier.wait(timeout=10)
            service.upload_sorting(
                db_path,
                f"s{index}.csv",
                sort_bytes([(f"T{index}", 1, "厨余垃圾", (10 + index) * 1000)]),
            )
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    report = service.classification_view(db_path, "2026-10", "2026-10")
    assert report["pending_count"] == 0
    cats = {row["category"]: row for row in report["months"][0]["categories"]}
    assert cats["厨余垃圾"]["current_grams"] == sum(
        (10 + i) * 1000 for i in range(6)
    )
    assert report["months"][0]["matches_bottom"]


def test_failed_sorting_batch_leaves_no_rows(db_path):
    service.initialize(db_path, "2026-10")
    service.upload_batch(db_path, "wb.csv", wb_bytes([("A1", "2026-10", "城东", 100, 1)]))
    service.upload_sorting(
        db_path, "ok.csv", sort_bytes([("A1", 1, "厨余垃圾", 100000)])
    )

    with __import__("pytest").raises(BatchRejected):
        service.upload_sorting(
            db_path,
            "bad.csv",
            sort_bytes(
                [
                    ("A1", 1, "其他垃圾", 10),  # sum mismatch -> whole batch back
                ]
            ),
        )

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT category, grams FROM sorting_splits WHERE ticket_no = 'A1'"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("厨余垃圾", 100000)]
    # Only the successful batch has a record.
    conn = sqlite3.connect(db_path)
    try:
        names = [
            row[0]
            for row in conn.execute("SELECT filename FROM sorting_batches").fetchall()
        ]
    finally:
        conn.close()
    assert names == ["ok.csv"]


def test_sorting_exchange_concurrent_with_seal_keeps_totals_consistent(db_path):
    service.initialize(db_path, "2026-10")
    service.upload_batch(db_path, "wb.csv", wb_bytes([("A1", "2026-10", "城东", 100, 1)]))

    barrier = threading.Barrier(2)
    errors = []

    def sealer():
        try:
            barrier.wait(timeout=10)
            service.seal_month(db_path, "2026-10")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def sorter():
        try:
            barrier.wait(timeout=10)
            service.upload_sorting(
                db_path, "s.csv", sort_bytes([("A1", 1, "厨余垃圾", 100000)])
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=sealer), threading.Thread(target=sorter)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, errors

    # Sealed bottom table stays street-based and immutable.
    snapshot = service.get_snapshot(db_path, "2026-10")
    assert snapshot["totals"]["total_grams"] == 100000

    # Whatever the commit order, every report month still cross-checks.
    for month in ("2026-10", "2026-11"):
        report = service.classification_view(db_path, month, month)
        assert report["months"][0]["matches_bottom"]

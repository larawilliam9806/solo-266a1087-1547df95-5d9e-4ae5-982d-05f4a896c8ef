"""HTTP tests for sorting uploads and the category monthly table."""

import io


def csrf(client):
    client.get("/")
    with client.session_transaction() as sess:
        return sess["csrf"]


def wb_csv(rows):
    text = "单号,清运月,街道,净重（千克）,修订号\n"
    text += "".join(",".join(str(p) for p in r) + "\n" for r in rows)
    return text.encode("utf-8")


def sort_csv(rows):
    text = "单号,修订号,类别,克重\n"
    text += "".join(",".join(str(p) for p in r) + "\n" for r in rows)
    return text.encode("utf-8")


def post_wb(client, rows):
    return client.post(
        "/api/upload",
        data={
            "file": (io.BytesIO(wb_csv(rows)), "wb.csv"),
            "target_month": "2026-10",
        },
        headers={"X-CSRF-Token": csrf(client)},
        content_type="multipart/form-data",
    )


def post_sort(client, rows):
    return client.post(
        "/api/sorting",
        data={"file": (io.BytesIO(sort_csv(rows)), "sorting.csv")},
        headers={"X-CSRF-Token": csrf(client)},
        content_type="multipart/form-data",
    )


def seed(client):
    resp = post_wb(client, [("A1", "2026-10", "城东", 100, 1)])
    assert resp.status_code == 200, resp.get_data(as_text=True)


def test_sorting_requires_csrf(client, initialized):
    response = client.post(
        "/api/sorting",
        data={"file": (io.BytesIO(sort_csv([("A1", 1, "厨余", 1)])), "s.csv")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400


def test_sorting_upload_ok_and_classification_api(client, initialized):
    seed(client)
    response = post_sort(client, [("A1", 1, "厨余垃圾", 60000), ("A1", 1, "其他垃圾", 40000)])
    assert response.status_code == 200, response.get_data(as_text=True)
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["summary"]["tickets"] == 1

    report = client.get("/api/classification?start=2026-10&end=2026-10").get_json()
    cats = {row["category"]: row for row in report["months"][0]["categories"]}
    assert cats["厨余垃圾"]["current_grams"] == 60000
    assert cats["其他垃圾"]["current_grams"] == 40000
    assert report["months"][0]["matches_bottom"] is True
    assert report["pending_count"] == 0


def test_sorting_rejection_returns_line_details(client, initialized):
    seed(client)
    response = post_sort(client, [("A1", 1, "厨余垃圾", 99999)])  # sum mismatch
    assert response.status_code == 409
    payload = response.get_json()
    assert payload["code"] == "batch_rejected"
    assert payload["details"][0]["ticket_no"] == "A1"


def test_uncategorized_shown_on_classify_page(client, initialized):
    seed(client)
    page = client.get("/classify?start=2026-10&end=2026-10")
    assert page.status_code == 200
    text = page.get_data(as_text=True)
    assert "分类月表" in text
    assert "未分类" in text
    assert "A1" in text  # pending list shows the ticket


def test_classify_page_renders_after_split(client, initialized):
    seed(client)
    post_sort(client, [("A1", 1, "厨余垃圾", 100000)])
    text = client.get("/classify?start=2026-10&end=2026-10").get_data(as_text=True)
    assert "厨余垃圾" in text
    assert "原底表合计核对" in text
    # Pending section shows the empty-state wording.
    assert "没有待补分拣" in text


def test_classification_api_requires_bounds(client, initialized):
    response = client.get("/api/classification?start=2026-10")
    assert response.status_code == 422


def test_sorting_template_download(client, initialized):
    response = client.get("/sorting-template.csv")
    assert response.status_code == 200
    assert "单号" in response.get_data(as_text=True)
    assert "克重" in response.get_data(as_text=True)


def test_nav_links_to_classify(client, initialized):
    text = client.get("/").get_data(as_text=True)
    assert "/classify" in text


def test_classify_redirects_to_setup_when_uninitialized(client):
    text = client.get("/classify").get_data(as_text=True)
    assert "选定首个待封月" in text


def test_sorting_batch_listed_on_state(client, initialized):
    seed(client)
    post_sort(client, [("A1", 1, "厨余垃圾", 100000)])
    state = client.get("/api/state").get_json()
    assert state["sorting_batches"][0]["ticket_count"] == 1
    assert state["sorting_batches"][0]["row_count"] == 1


def test_sealed_download_unaffected_by_sorting(client, initialized):
    seed(client)
    client.post(
        "/seal",
        data={"csrf": csrf(client), "month": "2026-10"},
    )
    post_sort(client, [("A1", 1, "厨余垃圾", 100000)])
    download = client.get("/month/2026-10/download")
    text = download.get_data(as_text=True)
    assert "厨余垃圾" not in text  # bottom table has streets, not categories
    assert "城东" in text

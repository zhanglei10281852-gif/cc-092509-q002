from __future__ import annotations

import base64

from fastapi.testclient import TestClient


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def create_project(client, headers, code="INV-2026-09", title="低温柔性密封结构"):
    response = client.post(
        "/api/disclosure-packages/projects",
        headers=headers,
        json={"project_code": code, "title": title},
    )
    assert response.status_code == 201, response.text
    return response.json()


def submit(client, headers, project_code, label, carriers, note=""):
    response = client.post(
        f"/api/disclosure-packages/projects/{project_code}/submissions",
        headers=headers,
        json={"label": label, "note": note, "carriers": carriers},
    )
    assert response.status_code == 201, response.text
    return response.json()


def reopen_client():
    """以同一数据库文件重新启动一个服务实例，模拟重启后从文件恢复。"""
    from app.main import app

    test_client = TestClient(app)
    test_client.__enter__()
    return test_client


def test_draft_accepts_supplements_and_preserves_order(client, admin):
    headers = admin["headers"]
    create_project(client, headers)
    first = submit(
        client, headers, "INV-2026-09", "首批：交底书与图纸",
        [
            {"file_name": "交底书-v1.docx", "carrier_kind": "专利交底书", "content_base64": b64("disclosure-body")},
            {"file_name": "装配图.dwg", "carrier_kind": "图纸", "content_base64": b64("drawing-a")},
        ],
    )
    assert first["version"]["version_no"] == 1
    assert first["batch"]["batch_seq"] == 1
    assert [c["sequence_no"] for c in first["carriers"]] == [1, 2]

    # 草稿阶段补交一页：进入同一版本的新批次，提交顺序继续递增
    second = submit(
        client, headers, "INV-2026-09", "补交：实验记录单页",
        [{"file_name": "实验记录-第7页.pdf", "carrier_kind": "实验记录", "content_base64": b64("lab-page-7")}],
    )
    assert second["version"]["version_no"] == 1
    assert second["batch"]["batch_seq"] == 2
    assert second["carriers"][0]["sequence_no"] == 3

    detail = client.get("/api/disclosure-packages/projects/INV-2026-09/versions/1", headers=headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["carrier_count"] == 3
    assert [c["sequence_no"] for c in body["carriers"]] == [1, 2, 3]
    assert [c["batch_seq"] for c in body["carriers"]] == [1, 1, 2]
    assert [c["file_name"] for c in body["carriers"]] == ["交底书-v1.docx", "装配图.dwg", "实验记录-第7页.pdf"]
    assert [b["batch_seq"] for b in body["batches"]] == [1, 2]


def test_duplicate_digest_returns_original_record_even_when_renamed(client, admin):
    headers = admin["headers"]
    create_project(client, headers)
    payload_body = b64("same-content")
    submit(
        client, headers, "INV-2026-09", "首批",
        [{"file_name": "原名.docx", "carrier_kind": "专利交底书", "content_base64": payload_body}],
    )
    # 同一内容改名后再次上传，且与新材料混在同一批次
    replay = submit(
        client, headers, "INV-2026-09", "重复批次",
        [
            {"file_name": "法务改名副本.docx", "carrier_kind": "专利交底书", "content_base64": payload_body},
            {"file_name": "新增图纸.dwg", "carrier_kind": "图纸", "content_base64": b64("new-drawing")},
        ],
    )
    assert replay["added"] == 1
    assert replay["duplicated"] == 1
    statuses = {c["file_name"]: c["status"] for c in replay["carriers"]}
    assert statuses["法务改名副本.docx"] == "duplicate"
    assert statuses["新增图纸.dwg"] == "added"

    detail = client.get("/api/disclosure-packages/projects/INV-2026-09/versions/1", headers=headers).json()
    # 重复载体不占用新的提交顺序，版本内仍只有 2 个不同载体
    assert [c["sequence_no"] for c in detail["carriers"]] == [1, 2]
    original = detail["carriers"][0]
    replayed = replay["carriers"][0]
    assert replayed["carrier_id"] == original["carrier_id"] == detail["carriers"][0]["carrier_id"]


def test_freeze_blocks_overwrite_and_new_material_forms_next_version(client, admin):
    headers = admin["headers"]
    create_project(client, headers)
    submit(
        client, headers, "INV-2026-09", "送审材料",
        [
            {"file_name": "交底书.docx", "carrier_kind": "专利交底书", "content_base64": b64("body")},
            {"file_name": "图.dwg", "carrier_kind": "图纸", "content_base64": b64("drawing")},
        ],
    )
    frozen = client.post(
        "/api/disclosure-packages/projects/INV-2026-09/submit",
        headers=headers, json={"note": "送专利律师"},
    )
    assert frozen.status_code == 200, frozen.text
    assert frozen.json()["version"]["status"] == "submitted"
    assert frozen.json()["version"]["version_no"] == 1
    assert frozen.json()["carrier_count"] == 2
    assert len(frozen.json()["manifest_digest"]) == 64

    # 重复送审被拒绝
    again = client.post("/api/disclosure-packages/projects/INV-2026-09/submit", headers=headers, json={})
    assert again.status_code == 409

    # 冻结后的新材料形成 v2，先按原顺序继承 v1 快照
    followup = submit(
        client, headers, "INV-2026-09", "冻结后补：修订实验记录",
        [{"file_name": "实验记录-修订.pdf", "carrier_kind": "实验记录", "content_base64": b64("lab-rev2")}],
    )
    assert followup["version"]["version_no"] == 2
    assert followup["inherited_from_version_no"] == 1
    assert followup["carriers"][0]["sequence_no"] == 3

    v1 = client.get("/api/disclosure-packages/projects/INV-2026-09/versions/1", headers=headers).json()
    v2 = client.get("/api/disclosure-packages/projects/INV-2026-09/versions/2", headers=headers).json()
    # 旧版本冻结内容不变
    assert v1["version"]["status"] == "submitted"
    assert v1["carrier_count"] == 2
    assert [c["file_name"] for c in v1["carriers"]] == ["交底书.docx", "图.dwg"]
    # 新版本包含完整材料关系：快照 + 新增
    assert v2["carrier_count"] == 3
    assert [c["inclusion_type"] for c in v2["carriers"]] == ["snapshot", "snapshot", "received"]
    assert [c["sequence_no"] for c in v2["carriers"]] == [1, 2, 3]
    assert [c["batch_seq"] for c in v2["carriers"]] == [1, 1, 2]
    # 快照成员与新版本成员指向同一物理载体
    assert v2["carriers"][0]["carrier_id"] == v1["carriers"][0]["carrier_id"]

    versions = client.get("/api/disclosure-packages/projects/INV-2026-09/versions", headers=headers).json()
    assert [v["version_no"] for v in versions["versions"]] == [1, 2]
    assert [v["status"] for v in versions["versions"]] == ["submitted", "draft"]


def test_manifest_digest_is_recomputable(client, admin):
    headers = admin["headers"]
    create_project(client, headers)
    submit(
        client, headers, "INV-2026-09", "首批",
        [{"file_name": "交底书.docx", "carrier_kind": "专利交底书", "content_base64": b64("abc")}],
    )
    frozen = client.post("/api/disclosure-packages/projects/INV-2026-09/submit", headers=headers, json={}).json()

    verify = client.get(f"/api/disclosure-packages/carriers/{frozen['version']['id']}", headers=headers)
    # version id 不是 carrier id，应当 404
    assert verify.status_code == 404

    v1 = client.get("/api/disclosure-packages/projects/INV-2026-09/versions/1", headers=headers).json()
    carrier_id = v1["carriers"][0]["carrier_id"]
    checked = client.get(f"/api/disclosure-packages/carriers/{carrier_id}/verify", headers=headers)
    assert checked.status_code == 200, checked.text
    assert checked.json()["verified"] is True
    assert checked.json()["recomputed_digest"] == checked.json()["content_digest"]


def test_material_relations_and_audit_survive_restart(client, admin):
    headers = admin["headers"]
    create_project(client, headers)
    submit(
        client, headers, "INV-2026-09", "首批",
        [{"file_name": "交底书.docx", "carrier_kind": "专利交底书", "content_base64": b64("body")}],
    )
    client.post("/api/disclosure-packages/projects/INV-2026-09/submit", headers=headers, json={"note": "送审"})
    submit(
        client, headers, "INV-2026-09", "补件",
        [{"file_name": "图纸.dwg", "carrier_kind": "图纸", "content_base64": b64("drawing")}],
    )

    restarted = reopen_client()
    detail1 = restarted.get("/api/disclosure-packages/projects/INV-2026-09/versions/1", headers=headers)
    detail2 = restarted.get("/api/disclosure-packages/projects/INV-2026-09/versions/2", headers=headers)
    assert detail1.json()["version"]["status"] == "submitted"
    assert detail1.json()["carrier_count"] == 1
    assert detail2.json()["carrier_count"] == 2
    assert [c["inclusion_type"] for c in detail2.json()["carriers"]] == ["snapshot", "received"]

    events = restarted.get("/api/disclosure-packages/projects/INV-2026-09/events", headers=headers).json()["events"]
    event_types = [e["event_type"] for e in events]
    assert "project.created" in event_types
    assert "version.created" in event_types
    assert event_types.count("version.created") == 2
    assert "version.submitted" in event_types
    assert "carrier.added" in event_types

    audit = restarted.get(
        "/api/audit?resource_type=disclosure_version&size=50", headers=headers
    ).json()
    actions = {item["action"] for item in audit["data"]}
    assert "disclosure_package.carriers_submitted" in actions
    assert "disclosure_package.version_submitted" in actions


def test_pure_duplicate_after_freeze_does_not_create_version(client, admin):
    headers = admin["headers"]
    create_project(client, headers)
    submit(
        client, headers, "INV-2026-09", "送审材料",
        [{"file_name": "交底书.docx", "carrier_kind": "专利交底书", "content_base64": b64("body")}],
    )
    client.post("/api/disclosure-packages/projects/INV-2026-09/submit", headers=headers, json={})

    # 冻结后再次上传完全相同的内容（即使改名）：返回原记录，不派生 v2
    replay = submit(
        client, headers, "INV-2026-09", "冻结后重传",
        [{"file_name": "交底书-改名.docx", "carrier_kind": "专利交底书", "content_base64": b64("body")}],
    )
    assert replay["version"]["version_no"] == 1
    assert replay["version"]["status"] == "submitted"
    assert replay["batch"] is None
    assert replay["added"] == 0
    assert replay["duplicated"] == 1

    versions = client.get("/api/disclosure-packages/projects/INV-2026-09/versions", headers=headers).json()
    assert [v["version_no"] for v in versions["versions"]] == [1]


def test_submit_empty_package_rejected(client, admin):
    headers = admin["headers"]
    create_project(client, headers)
    response = client.post("/api/disclosure-packages/projects/INV-2026-09/submit", headers=headers, json={})
    assert response.status_code == 422


def test_submission_to_unknown_project_rejected(client, admin):
    response = client.post(
        "/api/disclosure-packages/projects/NO-SUCH/submissions",
        headers=admin["headers"],
        json={"label": "首批", "carriers": [{"file_name": "a.docx", "content_base64": b64("x")}]},
    )
    assert response.status_code == 422

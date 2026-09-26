from __future__ import annotations

import hashlib

from app.database import close_connection
from app.main import app
from fastapi.testclient import TestClient


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _carrier(kind: str, file_name: str, payload: bytes, *, media_type: str = "application/pdf") -> dict:
    return {
        "carrier_kind": kind,
        "file_name": file_name,
        "media_type": media_type,
        "size_bytes": len(payload),
        "content_digest": _digest(payload),
    }


DISCLOSURE = _carrier("patent_disclosure", "交底书-v1.pdf", b"disclosure body")
LAB_RECORD = _carrier("laboratory_record", "实验记录-批次一.xlsx", b"lab notes")
DRAWING = _carrier("design_drawing", "图纸A.dwg", b"cad data")
SUPPLEMENT = _carrier("laboratory_record", "补交一页.pdf", b"extra page")


def _create_project(client, headers, code="INV-2026-001", title="新型密封结构"):
    response = client.post(
        "/api/disclosure-packages/projects",
        headers=headers,
        json={"project_code": code, "title": title, "owner_department": "结构研发部"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _submit(client, headers, project_id, batch_code, carriers):
    response = client.post(
        f"/api/disclosure-packages/projects/{project_id}/carriers",
        headers=headers,
        json={"batch_code": batch_code, "carriers": carriers},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_first_batch_creates_draft_version_with_ordered_carriers(client, admin):
    project = _create_project(client, admin["headers"])
    result = _submit(client, admin["headers"], project["id"], "B1", [DISCLOSURE, LAB_RECORD])
    assert result["version"]["version_no"] == 1
    assert result["version"]["state"] == "draft"
    assert [item["sequence_no"] for item in result["carriers"]] == [1, 2]
    assert [item["file_name"] for item in result["carriers"]] == ["交底书-v1.pdf", "实验记录-批次一.xlsx"]
    assert all(item["duplicate"] is False for item in result["carriers"])


def test_duplicate_digest_returns_original_record_even_when_file_renamed(client, admin):
    project = _create_project(client, admin["headers"])
    first = _submit(client, admin["headers"], project["id"], "B1", [DISCLOSURE])
    renamed = {**DISCLOSURE, "file_name": "交底书-法务重命名.pdf"}
    second = _submit(client, admin["headers"], project["id"], "B2", [renamed])

    original = first["carriers"][0]
    repeated = second["carriers"][0]
    assert repeated["id"] == original["id"]
    assert repeated["duplicate"] is True
    assert repeated["file_name"] == "交底书-v1.pdf"
    assert repeated["submitted_file_name"] == "交底书-法务重命名.pdf"
    assert repeated["name_changed"] is True
    # 重复上传不产生新的顺序号，也没有改写首次登记的文件名。
    assert repeated["sequence_no"] == original["sequence_no"] == 1


def test_draft_allows_supplements_then_freezes_on_submission(client, admin):
    project = _create_project(client, admin["headers"])
    _submit(client, admin["headers"], project["id"], "B1", [DISCLOSURE, LAB_RECORD])
    supplement = _submit(client, admin["headers"], project["id"], "B2", [SUPPLEMENT])
    assert supplement["version"]["version_no"] == 1
    assert supplement["created_new_version"] is False
    assert [item["sequence_no"] for item in supplement["carriers"] if not item["duplicate"]] == [3]

    version_id = supplement["version"]["id"]
    frozen = client.post(
        f"/api/disclosure-packages/projects/{project['id']}/versions/{version_id}/submit",
        headers=admin["headers"],
    )
    assert frozen.status_code == 200, frozen.text
    body = frozen.json()
    assert body["version"]["state"] == "submitted"
    assert body["version"]["version_digest"].startswith("sha256:")
    assert body["carrier_count"] == 3
    assert [item["sequence_no"] for item in body["carriers"]] == [1, 2, 3]

    # 冻结后不能重复送审。
    again = client.post(
        f"/api/disclosure-packages/projects/{project['id']}/versions/{version_id}/submit",
        headers=admin["headers"],
    )
    assert again.status_code == 409


def test_frozen_version_is_immutable_and_new_material_starts_next_version(client, admin):
    project = _create_project(client, admin["headers"])
    first = _submit(client, admin["headers"], project["id"], "B1", [DISCLOSURE, LAB_RECORD])
    version_one_id = first["version"]["id"]
    client.post(
        f"/api/disclosure-packages/projects/{project['id']}/versions/{version_one_id}/submit",
        headers=admin["headers"],
    )

    second_batch = _submit(client, admin["headers"], project["id"], "B2", [DRAWING])
    assert second_batch["created_new_version"] is True
    assert second_batch["version"]["version_no"] == 2
    assert second_batch["version"]["state"] == "draft"

    # v2 继承 v1 的全部载体并追加新材料，顺序号全局延续。
    v2 = client.get(
        f"/api/disclosure-packages/projects/{project['id']}/versions/{second_batch['version']['id']}",
        headers=admin["headers"],
    ).json()
    assert [item["sequence_no"] for item in v2["carriers"]] == [1, 2, 3]
    assert [item["batch_code"] for item in v2["carriers"]] == ["B1", "B1", "B2"]
    assert [item["first_version_no"] for item in v2["carriers"]] == [1, 1, 2]

    # v1 仍然只包含送审时的两件载体，未被覆盖。
    v1 = client.get(
        f"/api/disclosure-packages/projects/{project['id']}/versions/{version_one_id}",
        headers=admin["headers"],
    ).json()
    assert v1["version"]["state"] == "submitted"
    assert [item["content_digest"] for item in v1["carriers"]] == [
        DISCLOSURE["content_digest"], LAB_RECORD["content_digest"],
    ]
    assert v1["carrier_count"] == 2


def test_reupload_after_freeze_without_new_material_replays_without_new_version(client, admin):
    project = _create_project(client, admin["headers"])
    first = _submit(client, admin["headers"], project["id"], "B1", [DISCLOSURE])
    client.post(
        f"/api/disclosure-packages/projects/{project['id']}/versions/{first['version']['id']}/submit",
        headers=admin["headers"],
    )
    replayed = _submit(client, admin["headers"], project["id"], "B2", [{**DISCLOSURE, "file_name": "另一个名字.pdf"}])
    assert replayed["replayed"] is True
    assert replayed["created_new_version"] is False
    assert replayed["version"]["version_no"] == 1
    assert replayed["carriers"][0]["id"] == first["carriers"][0]["id"]

    detail = client.get(f"/api/disclosure-packages/projects/{project['id']}", headers=admin["headers"]).json()
    assert [version["version_no"] for version in detail["versions"]] == [1]


def test_same_batch_rejects_internal_digest_duplicates(client, admin):
    project = _create_project(client, admin["headers"])
    response = client.post(
        f"/api/disclosure-packages/projects/{project['id']}/carriers",
        headers=admin["headers"],
        json={"batch_code": "B1", "carriers": [DISCLOSURE, {**DISCLOSURE, "file_name": "别名.pdf"}]},
    )
    assert response.status_code == 422


def test_versions_and_audit_trail_survive_service_restart(client, admin, tmp_path):
    project = _create_project(client, admin["headers"])
    first = _submit(client, admin["headers"], project["id"], "B1", [DISCLOSURE, LAB_RECORD])
    client.post(
        f"/api/disclosure-packages/projects/{project['id']}/versions/{first['version']['id']}/submit",
        headers=admin["headers"],
    )
    _submit(client, admin["headers"], project["id"], "B2", [DRAWING])

    # 模拟服务重启：关闭当前连接并重新创建应用与客户端，仍指向同一个数据库文件。
    close_connection()
    with TestClient(app) as restarted:
        login = restarted.post(
            "/api/auth/login",
            json={"username": "admin", "password": "Admin!23456", "client_label": "tests"},
        )
        headers = {"Authorization": f"Bearer {login.json()['token']}"}

        detail = restarted.get(f"/api/disclosure-packages/projects/{project['id']}", headers=headers).json()
        assert [version["version_no"] for version in detail["versions"]] == [1, 2]
        assert [version["state"] for version in detail["versions"]] == ["submitted", "draft"]
        v1 = restarted.get(
            f"/api/disclosure-packages/projects/{project['id']}/versions/by-no/1", headers=headers
        ).json()
        v2 = restarted.get(
            f"/api/disclosure-packages/projects/{project['id']}/versions/by-no/2", headers=headers
        ).json()
        assert v1["version"]["state"] == "submitted"
        assert [item["sequence_no"] for item in v1["carriers"]] == [1, 2]
        assert [item["sequence_no"] for item in v2["carriers"]] == [1, 2, 3]
        assert v2["carriers"][2]["file_name"] == "图纸A.dwg"

        audit = restarted.get(
            "/api/audit?resource_type=disclosure_version&size=50", headers=headers
        ).json()
        actions = {item["action"] for item in audit["data"]}
        assert "disclosure_version.submitted" in actions
        assert "disclosure_version.carriers_submitted" in actions
        # 送审审计记录了版本状态变化。
        submitted_event = next(item for item in audit["data"] if item["action"] == "disclosure_version.submitted")
        assert "draft" in submitted_event["before_json"]
        assert "submitted" in submitted_event["after_json"]


def test_version_digest_is_recomputable(client, admin):
    import json
    project = _create_project(client, admin["headers"])
    first = _submit(client, admin["headers"], project["id"], "B1", [DISCLOSURE])
    frozen = client.post(
        f"/api/disclosure-packages/projects/{project['id']}/versions/{first['version']['id']}/submit",
        headers=admin["headers"],
    ).json()
    carriers = frozen["carriers"]
    canonical = json.dumps(
        [
            {
                "sequence_no": item["sequence_no"],
                "carrier_kind": item["carrier_kind"],
                "file_name": item["file_name"],
                "media_type": item["media_type"],
                "size_bytes": item["size_bytes"],
                "content_digest": item["content_digest"],
            }
            for item in carriers
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    expected = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert frozen["version"]["version_digest"] == expected

"""发明交底包登记：按项目与提交批次接收多载体，内容摘要去重，
草稿阶段允许补件，送审后冻结版本；冻结后再补材料自动形成后续版本。

载体以 ``(项目, 内容摘要)`` 唯一归并：同一份交底书即使文件名变化，
重复上传也返回首次登记记录。版本之间不可变，新版本通过复制上一版
载体清单再加新材料形成，因此送审过的内容永远不会被覆盖。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import Principal
from app.services.audit import AuditService

CARRIER_KIND_NAMES: dict[str, str] = {
    "patent_disclosure": "专利交底书",
    "laboratory_record": "实验记录",
    "design_drawing": "工程图纸",
    "technical_document": "工艺技术文档",
    "source_media": "源代码介质",
    "other": "其他载体",
}


def _row(row: sqlite3.Row | None, message: str) -> dict[str, Any]:
    if row is None:
        raise NotFoundError(message)
    return dict(row)


class DisclosurePackageRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    # ---- 项目 ----

    def create_project(self, data: dict[str, Any], user_id: int, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO invention_projects(project_code,title,owner_department,created_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (data["project_code"].strip(), data["title"].strip(), data["owner_department"].strip(), user_id, now, now),
        )
        return self.get_project(cursor.lastrowid)

    def get_project(self, project_id: int) -> dict[str, Any]:
        return _row(
            self.connection.execute("SELECT * FROM invention_projects WHERE id=?", (project_id,)).fetchone(),
            "发明项目不存在",
        )

    def list_projects(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM invention_projects ORDER BY id").fetchall()]

    # ---- 版本 ----

    def create_version(self, project_id: int, version_no: int, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO disclosure_versions(project_id,version_no,state,created_at,updated_at)
               VALUES(?,?,'draft',?,?)""",
            (project_id, version_no, now, now),
        )
        return self.get_version(project_id, cursor.lastrowid)

    def get_version(self, project_id: int, version_id: int) -> dict[str, Any]:
        return _row(
            self.connection.execute(
                "SELECT * FROM disclosure_versions WHERE id=? AND project_id=?",
                (version_id, project_id),
            ).fetchone(),
            "交底包版本不存在",
        )

    def get_version_by_no(self, project_id: int, version_no: int) -> dict[str, Any]:
        return _row(
            self.connection.execute(
                "SELECT * FROM disclosure_versions WHERE project_id=? AND version_no=?",
                (project_id, version_no),
            ).fetchone(),
            "交底包版本不存在",
        )

    def latest_version(self, project_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM disclosure_versions WHERE project_id=? ORDER BY version_no DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_versions(self, project_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT v.*, COUNT(c.carrier_id) AS carrier_count
               FROM disclosure_versions v
               LEFT JOIN disclosure_version_carriers c ON c.version_id=v.id
               WHERE v.project_id=?
               GROUP BY v.id ORDER BY v.version_no""",
            (project_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def copy_carriers(self, source_version_id: int, target_version_id: int) -> None:
        """创建后续版本时原样继承上一版的载体清单、顺序与提交批次。"""
        self.connection.execute(
            """INSERT INTO disclosure_version_carriers(
                   version_id,carrier_id,project_id,sequence_no,batch_code,first_version_no,added_by,added_at
               )
               SELECT ?,carrier_id,project_id,sequence_no,batch_code,first_version_no,added_by,added_at
               FROM disclosure_version_carriers WHERE version_id=?""",
            (target_version_id, source_version_id),
        )

    def freeze_version(self, version: dict[str, Any], digest: str, user_id: int, now: str) -> dict[str, Any]:
        self.connection.execute(
            """UPDATE disclosure_versions
               SET state='submitted',version_digest=?,submitted_by=?,submitted_at=?,updated_at=?
               WHERE id=?""",
            (digest, user_id, now, now, version["id"]),
        )
        return self.get_version(version["project_id"], version["id"])

    # ---- 载体 ----

    def find_carrier(self, project_id: int, content_digest: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM disclosure_carriers WHERE project_id=? AND content_digest=?",
            (project_id, content_digest),
        ).fetchone()
        return dict(row) if row else None

    def create_carrier(self, project_id: int, item: dict[str, Any], batch_code: str, user_id: int, now: str, first_version_no: int) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO disclosure_carriers(
                   project_id,carrier_kind,file_name,media_type,size_bytes,digest_algorithm,
                   content_digest,first_batch_code,uploaded_by,created_at
               ) VALUES(?,?,?,?,?,'sha256',?,?,?,?)""",
            (
                project_id, item["carrier_kind"], item["file_name"], item.get("media_type", ""),
                item.get("size_bytes", 0), item["content_digest"], batch_code, user_id, now,
            ),
        )
        return dict(self.connection.execute("SELECT * FROM disclosure_carriers WHERE id=?", (cursor.lastrowid,)).fetchone())

    def next_sequence(self, project_id: int) -> int:
        return int(
            self.connection.execute(
                "SELECT COALESCE(MAX(sequence_no),0) FROM disclosure_version_carriers WHERE project_id=?",
                (project_id,),
            ).fetchone()[0]
        ) + 1

    def link_carrier(self, version_id: int, carrier_id: int, project_id: int, sequence_no: int, batch_code: str, first_version_no: int, user_id: int, now: str) -> None:
        self.connection.execute(
            """INSERT INTO disclosure_version_carriers(
                   version_id,carrier_id,project_id,sequence_no,batch_code,first_version_no,added_by,added_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (version_id, carrier_id, project_id, sequence_no, batch_code, first_version_no, user_id, now),
        )

    def carrier_in_version(self, version_id: int, carrier_id: int) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM disclosure_version_carriers WHERE version_id=? AND carrier_id=?",
            (version_id, carrier_id),
        ).fetchone() is not None

    def version_carriers(self, version_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT c.id,c.carrier_kind,c.file_name,c.media_type,c.size_bytes,c.digest_algorithm,
                      c.content_digest,c.first_batch_code,c.uploaded_by,c.created_at,
                      l.sequence_no,l.batch_code,l.first_version_no,l.added_by,l.added_at
               FROM disclosure_version_carriers l
               JOIN disclosure_carriers c ON c.id=l.carrier_id
               WHERE l.version_id=? ORDER BY l.sequence_no""",
            (version_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["carrier_kind_name"] = CARRIER_KIND_NAMES.get(item["carrier_kind"], item["carrier_kind"])
            result.append(item)
        return result


class DisclosurePackageService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repo = DisclosurePackageRepository(connection)
        self.audit = AuditService(connection, self.clock)

    def create_project(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("disclosure_packages.manage")
        code = data["project_code"].strip()
        row = self.connection.execute("SELECT id FROM invention_projects WHERE project_code=?", (code,)).fetchone()
        if row:
            raise ConflictError("发明项目编码已经存在")
        now = to_storage(self.clock.now())
        project = self.repo.create_project(data, principal.user_id, now)
        self.audit.record(principal, "disclosure_project.create", "invention_project", str(project["id"]), after=project)
        return project

    def list_projects(self, principal: Principal) -> list[dict[str, Any]]:
        principal.require("dossiers.read")
        return self.repo.list_projects()

    def project_detail(self, principal: Principal, project_id: int) -> dict[str, Any]:
        principal.require("dossiers.read")
        project = self.repo.get_project(project_id)
        return {"project": project, "versions": self.repo.list_versions(project_id)}

    def submit_carriers(self, principal: Principal, project_id: int, data: dict[str, Any]) -> dict[str, Any]:
        """按批次接收载体；草稿补件、冻结后新材料开后续版本、纯重复返回原记录。"""
        principal.require("disclosure_packages.manage")
        project = self.repo.get_project(project_id)
        batch_code = data["batch_code"].strip()
        raw_items = data["carriers"]
        if not batch_code:
            raise ValidationError("提交批次编码不能为空")

        now = to_storage(self.clock.now())
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_items:
            item = dict(raw)
            item["content_digest"] = item["content_digest"].strip().lower()
            if item["content_digest"] in seen:
                raise ValidationError("同一批次内存在内容摘要相同的载体")
            seen.add(item["content_digest"])
            items.append(item)

        latest = self.repo.latest_version(project_id)
        replayed = False
        created_new_version = False
        if latest is None:
            version = self.repo.create_version(project_id, 1, now)
        elif latest["state"] == "submitted":
            already_submitted = self._all_in_version(project_id, latest["id"], items)
            if already_submitted:
                # 整批都是送审版本里已有的载体：原样返回，不产生新版本。
                version = latest
                replayed = True
            else:
                version = self.repo.create_version(project_id, latest["version_no"] + 1, now)
                self.repo.copy_carriers(latest["id"], version["id"])
                created_new_version = True
        else:
            version = latest

        results: list[dict[str, Any]] = []
        new_carrier_ids: list[int] = []
        duplicate_ids: list[int] = []
        for item in items:
            carrier = self.repo.find_carrier(project_id, item["content_digest"])
            if carrier is None:
                carrier = self.repo.create_carrier(
                    project_id, item, batch_code, principal.user_id, now, version["version_no"]
                )
                sequence_no = self.repo.next_sequence(project_id)
                self.repo.link_carrier(
                    version["id"], carrier["id"], project_id, sequence_no,
                    batch_code, version["version_no"], principal.user_id, now,
                )
                new_carrier_ids.append(carrier["id"])
                results.append(self._carrier_result(carrier, sequence_no, batch_code, now, duplicate=False, submitted_file_name=item["file_name"]))
                continue

            duplicate_ids.append(carrier["id"])
            if self.repo.carrier_in_version(version["id"], carrier["id"]):
                link = self.connection.execute(
                    "SELECT sequence_no,batch_code,added_at FROM disclosure_version_carriers WHERE version_id=? AND carrier_id=?",
                    (version["id"], carrier["id"]),
                ).fetchone()
                results.append(self._carrier_result(carrier, link["sequence_no"], link["batch_code"], link["added_at"], duplicate=True, submitted_file_name=item["file_name"]))
            else:
                # 历史载体不在当前版本（防御分支）：按补件加入并延续全局顺序。
                sequence_no = self.repo.next_sequence(project_id)
                self.repo.link_carrier(
                    version["id"], carrier["id"], project_id, sequence_no,
                    batch_code, version["version_no"], principal.user_id, now,
                )
                results.append(self._carrier_result(carrier, sequence_no, batch_code, now, duplicate=True, submitted_file_name=item["file_name"]))

        self.connection.execute(
            "UPDATE disclosure_versions SET updated_at=? WHERE id=?",
            (now, version["id"]),
        )
        version = self.repo.get_version(project_id, version["id"])
        self.audit.record(
            principal,
            "disclosure_version.carriers_submitted",
            "disclosure_version",
            str(version["id"]),
            after={"version_no": version["version_no"], "state": version["state"]},
            metadata={
                "project_id": project_id,
                "project_code": project["project_code"],
                "batch_code": batch_code,
                "version_no": version["version_no"],
                "created_new_version": created_new_version,
                "replayed": replayed,
                "new_carrier_ids": new_carrier_ids,
                "duplicate_carrier_ids": duplicate_ids,
            },
        )
        return {
            "project": project,
            "version": version,
            "batch_code": batch_code,
            "created_new_version": created_new_version,
            "replayed": replayed,
            "carriers": results,
        }

    def submit_version(self, principal: Principal, project_id: int, version_id: int) -> dict[str, Any]:
        """送审：冻结版本并固化版本摘要，此后该版本内容不可变。"""
        principal.require("disclosure_packages.manage")
        self.repo.get_project(project_id)
        version = self.repo.get_version(project_id, version_id)
        if version["state"] != "draft":
            raise ConflictError("该版本已经送审冻结，不能重复送审")
        carriers = self.repo.version_carriers(version["id"])
        if not carriers:
            raise ConflictError("版本还没有任何载体，不能送审")
        digest = self._version_digest(carriers)
        before = dict(version)
        now = to_storage(self.clock.now())
        frozen = self.repo.freeze_version(version, digest, principal.user_id, now)
        self.audit.record(
            principal,
            "disclosure_version.submitted",
            "disclosure_version",
            str(version["id"]),
            before=before,
            after=frozen,
            metadata={"project_id": project_id, "version_no": frozen["version_no"], "carrier_count": len(carriers)},
        )
        return self.get_version(principal, project_id, version_id)

    def get_version(self, principal: Principal, project_id: int, version_id: int) -> dict[str, Any]:
        principal.require("dossiers.read")
        project = self.repo.get_project(project_id)
        version = self.repo.get_version(project_id, version_id)
        carriers = self.repo.version_carriers(version_id)
        return {
            "project": project,
            "version": version,
            "carrier_count": len(carriers),
            "carriers": carriers,
        }

    def get_version_by_no(self, principal: Principal, project_id: int, version_no: int) -> dict[str, Any]:
        version = self.repo.get_version_by_no(project_id, version_no)
        return self.get_version(principal, project_id, version["id"])

    def _all_in_version(self, project_id: int, version_id: int, items: list[dict[str, Any]]) -> bool:
        """送审后只有当整批载体都已包含在冻结版本中时，才视为纯重复上传。"""
        for item in items:
            existing = self.repo.find_carrier(project_id, item["content_digest"])
            if existing is None or not self.repo.carrier_in_version(version_id, existing["id"]):
                return False
        return True

    def _version_digest(self, carriers: list[dict[str, Any]]) -> str:
        """按提交顺序对全部载体摘要再做一次摘要，形成可复核的版本指纹。"""
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
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _carrier_result(
        self,
        carrier: dict[str, Any],
        sequence_no: int,
        batch_code: str,
        added_at: str,
        *,
        duplicate: bool,
        submitted_file_name: str,
    ) -> dict[str, Any]:
        return {
            "id": carrier["id"],
            "sequence_no": sequence_no,
            "carrier_kind": carrier["carrier_kind"],
            "carrier_kind_name": CARRIER_KIND_NAMES.get(carrier["carrier_kind"], carrier["carrier_kind"]),
            "file_name": carrier["file_name"],
            "submitted_file_name": submitted_file_name,
            "name_changed": submitted_file_name != carrier["file_name"],
            "media_type": carrier["media_type"],
            "size_bytes": carrier["size_bytes"],
            "digest_algorithm": carrier["digest_algorithm"],
            "content_digest": carrier["content_digest"],
            "first_batch_code": carrier["first_batch_code"],
            "batch_code": batch_code,
            "added_at": added_at,
            "uploaded_by": carrier["uploaded_by"],
            "duplicate": duplicate,
        }

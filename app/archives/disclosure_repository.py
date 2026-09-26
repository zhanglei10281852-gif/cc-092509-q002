from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.errors import ConflictError, NotFoundError


def _row(row: sqlite3.Row | None, message: str) -> dict[str, Any]:
    if row is None:
        raise NotFoundError(message)
    return dict(row)


class DisclosurePackageRepository:
    """交底包（发明项目-版本-提交批次-载体）的持久化访问。"""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    # ---- 发明项目 ----

    def create_project(self, project_code: str, title: str, user_id: int, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO disclosure_projects(project_code,title,created_by,created_at,updated_at) VALUES(?,?,?,?,?)",
            (project_code, title, user_id, now, now),
        )
        return self.get_project(cursor.lastrowid)

    def get_project(self, project_id: int) -> dict[str, Any]:
        return _row(
            self.connection.execute("SELECT * FROM disclosure_projects WHERE id=?", (project_id,)).fetchone(),
            "发明项目不存在",
        )

    def get_project_by_code(self, project_code: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM disclosure_projects WHERE project_code=?", (project_code,)
        ).fetchone()
        return dict(row) if row else None

    def touch_project(self, project_id: int, now: str) -> None:
        self.connection.execute(
            "UPDATE disclosure_projects SET updated_at=? WHERE id=?", (now, project_id)
        )

    # ---- 版本 ----

    def create_version(
        self,
        project_id: int,
        version_no: int,
        created_by: int,
        now: str,
        *,
        based_on_version_id: int | None = None,
    ) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO disclosure_versions(project_id,version_no,status,based_on_version_id,created_by,created_at,updated_at)
               VALUES(?,?,'draft',?,?,?,?)""",
            (project_id, version_no, based_on_version_id, created_by, now, now),
        )
        return self.get_version(cursor.lastrowid)

    def get_version(self, version_id: int) -> dict[str, Any]:
        version = _row(
            self.connection.execute("SELECT * FROM disclosure_versions WHERE id=?", (version_id,)).fetchone(),
            "交底包版本不存在",
        )
        return version

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
        return [
            dict(row)
            for row in self.connection.execute(
                """SELECT v.*,
                          (SELECT COUNT(*) FROM disclosure_carrier_memberships m WHERE m.version_id=v.id) AS carrier_count
                   FROM disclosure_versions v WHERE v.project_id=? ORDER BY v.version_no""",
                (project_id,),
            ).fetchall()
        ]

    def freeze_version(self, version_id: int, manifest: list[dict[str, Any]], digest: str, submitted_by: int, now: str) -> dict[str, Any]:
        updated = self.connection.execute(
            """UPDATE disclosure_versions
               SET status='submitted',manifest_json=?,manifest_digest=?,submitted_by=?,submitted_at=?,updated_at=?
               WHERE id=? AND status='draft'""",
            (json.dumps(manifest, ensure_ascii=False, sort_keys=True), digest, submitted_by, now, now, version_id),
        )
        if updated.rowcount != 1:
            raise ConflictError("版本已经送审或已被更新，请刷新后重试")
        return self.get_version(version_id)

    # ---- 提交批次 ----

    def next_batch_seq(self, version_id: int) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(batch_seq),0)+1 FROM disclosure_submission_batches WHERE version_id=?",
            (version_id,),
        ).fetchone()
        return int(row[0])

    def create_batch(self, version_id: int, batch_seq: int, label: str, note: str, user_id: int, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO disclosure_submission_batches(version_id,batch_seq,label,note,received_by,received_at,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (version_id, batch_seq, label, note, user_id, now, now),
        )
        return _row(
            self.connection.execute(
                "SELECT * FROM disclosure_submission_batches WHERE id=?", (cursor.lastrowid,)
            ).fetchone(),
            "提交批次不存在",
        )

    def list_batches(self, version_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                """SELECT b.*,
                          (SELECT COUNT(*) FROM disclosure_carrier_memberships m WHERE m.batch_id=b.id) AS carrier_count
                   FROM disclosure_submission_batches b WHERE b.version_id=? ORDER BY b.batch_seq""",
                (version_id,),
            ).fetchall()
        ]

    # ---- 载体（内容寻址） ----

    def find_carrier(self, project_id: int, content_digest: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM disclosure_carriers WHERE project_id=? AND content_digest=?",
            (project_id, content_digest),
        ).fetchone()
        return dict(row) if row else None

    def create_carrier(self, values: dict[str, Any], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO disclosure_carriers(
                   project_id,carrier_kind,content_digest,digest_algorithm,content_size,content_blob,
                   media_type,first_file_name,first_submitted_by,first_submitted_at,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                values["project_id"], values["carrier_kind"], values["content_digest"], "sha256",
                values["content_size"], values["content_blob"], values["media_type"],
                values["first_file_name"], values["first_submitted_by"], now, now,
            ),
        )
        return _row(
            self.connection.execute(
                "SELECT * FROM disclosure_carriers WHERE id=?", (cursor.lastrowid,)
            ).fetchone(),
            "载体不存在",
        )

    def get_carrier(self, carrier_id: int) -> dict[str, Any]:
        return _row(
            self.connection.execute("SELECT * FROM disclosure_carriers WHERE id=?", (carrier_id,)).fetchone(),
            "载体不存在",
        )

    # ---- 版本成员关系（含提交顺序） ----

    def next_sequence(self, version_id: int) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(sequence_no),0)+1 FROM disclosure_carrier_memberships WHERE version_id=?",
            (version_id,),
        ).fetchone()
        return int(row[0])

    def add_membership(
        self,
        *,
        version_id: int,
        carrier_id: int,
        batch_id: int,
        sequence_no: int,
        file_name: str,
        inclusion_type: str,
        submitted_by: int,
        now: str,
    ) -> None:
        self.connection.execute(
            """INSERT INTO disclosure_carrier_memberships(
                   version_id,carrier_id,batch_id,sequence_no,file_name,inclusion_type,submitted_by,submitted_at,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (version_id, carrier_id, batch_id, sequence_no, file_name, inclusion_type, submitted_by, now, now),
        )

    def copy_snapshot(self, source_version_id: int, target_version_id: int, snapshot_batch_id: int, user_id: int, now: str) -> int:
        """把已冻结版本的全部成员按原顺序复制为新版本的快照成员，返回复制条数。"""
        rows = self.connection.execute(
            """SELECT carrier_id,sequence_no,file_name FROM disclosure_carrier_memberships
               WHERE version_id=? ORDER BY sequence_no""",
            (source_version_id,),
        ).fetchall()
        for row in rows:
            self.connection.execute(
                """INSERT INTO disclosure_carrier_memberships(
                       version_id,carrier_id,batch_id,sequence_no,file_name,inclusion_type,submitted_by,submitted_at,created_at
                   ) VALUES(?,?,?,?,?,'snapshot',?,?,?)""",
                (target_version_id, row["carrier_id"], snapshot_batch_id, row["sequence_no"], row["file_name"], user_id, now, now),
            )
        return len(rows)

    def list_memberships(self, version_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                """SELECT m.sequence_no,m.file_name,m.inclusion_type,m.submitted_at,
                          b.batch_seq,b.label AS batch_label,
                          c.id AS carrier_id,c.carrier_kind,c.content_digest,c.digest_algorithm,
                          c.content_size,c.media_type,c.first_file_name,c.first_submitted_at
                   FROM disclosure_carrier_memberships m
                   JOIN disclosure_carriers c ON c.id=m.carrier_id
                   JOIN disclosure_submission_batches b ON b.id=m.batch_id
                   WHERE m.version_id=? ORDER BY m.sequence_no""",
                (version_id,),
            ).fetchall()
        ]

    def membership_exists(self, version_id: int, carrier_id: int) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM disclosure_carrier_memberships WHERE version_id=? AND carrier_id=?",
            (version_id, carrier_id),
        ).fetchone() is not None

    # ---- 领域事件（审计轨迹） ----

    def append_event(
        self,
        *,
        project_id: int,
        version_id: int | None,
        carrier_id: int | None,
        event_type: str,
        actor_user_id: int | None,
        details: dict[str, Any],
        now: str,
    ) -> int:
        cursor = self.connection.execute(
            """INSERT INTO disclosure_package_events(project_id,version_id,carrier_id,event_type,actor_user_id,details_json,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (project_id, version_id, carrier_id, event_type, actor_user_id, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
        )
        return int(cursor.lastrowid)

    def list_events(self, project_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM disclosure_package_events WHERE project_id=? ORDER BY id",
            (project_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
from typing import Any

from app.archives.disclosure_repository import DisclosurePackageRepository
from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, ValidationError
from app.core.security import Principal
from app.services.audit import AuditService

SNAPSHOT_BATCH_LABEL_TEMPLATE = "继承版本 v{base_no} 送审快照"


def content_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def decode_content(content_base64: str) -> bytes:
    try:
        raw = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValidationError("载体内容必须是有效的 base64 编码") from exc
    if not raw:
        raise ValidationError("载体内容不能为空")
    return raw


class DisclosurePackageService:
    """交底包登记：按发明项目与提交批次接收多载体，草稿期补件，送审后冻结并派生新版本。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repo = DisclosurePackageRepository(connection)
        self.audit = AuditService(connection, self.clock)

    # ---- 发明项目 ----

    def create_project(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("disclosure_packages.write")
        if self.repo.get_project_by_code(data["project_code"]):
            raise ConflictError("发明项目编码已经存在")
        now = to_storage(self.clock.now())
        project = self.repo.create_project(data["project_code"], data["title"], principal.user_id, now)
        self.repo.append_event(
            project_id=project["id"], version_id=None, carrier_id=None,
            event_type="project.created", actor_user_id=principal.user_id,
            details={"project_code": project["project_code"], "title": project["title"]}, now=now,
        )
        self.audit.record(principal, "disclosure_package.project_created", "disclosure_project", str(project["id"]), after=project)
        return project

    def get_project(self, principal: Principal, project_code: str) -> dict[str, Any]:
        principal.require("disclosure_packages.read")
        project = self._require_project_by_code(project_code)
        project["versions"] = self.repo.list_versions(project["id"])
        return project

    def _require_project_by_code(self, project_code: str) -> dict[str, Any]:
        project = self.repo.get_project_by_code(project_code)
        if project is None:
            raise ValidationError("发明项目不存在，请先登记项目")
        return project

    # ---- 提交批次与载体 ----

    def submit_carriers(self, principal: Principal, project_code: str, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("disclosure_packages.write")
        project = self._require_project_by_code(project_code)
        now = to_storage(self.clock.now())

        decoded: list[tuple[dict[str, Any], bytes, str, dict[str, Any] | None]] = []
        for item in data["carriers"]:
            raw = decode_content(item["content_base64"])
            digest = content_digest(raw)
            decoded.append((item, raw, digest, self.repo.find_carrier(project["id"], digest)))

        version, batch, inherited_from = self._route_submission(project, data["label"], decoded, principal.user_id, now)

        results: list[dict[str, Any]] = []
        created = 0
        duplicated = 0
        for item, raw, digest, existing in decoded:
            carrier = existing
            if carrier is None:
                carrier = self.repo.create_carrier(
                    {
                        "project_id": project["id"],
                        "carrier_kind": item["carrier_kind"],
                        "content_digest": digest,
                        "content_size": len(raw),
                        "content_blob": raw,
                        "media_type": item.get("media_type", ""),
                        "first_file_name": item["file_name"],
                        "first_submitted_by": principal.user_id,
                    },
                    now,
                )
                self.repo.append_event(
                    project_id=project["id"], version_id=version["id"], carrier_id=carrier["id"],
                    event_type="carrier.registered", actor_user_id=principal.user_id,
                    details={"content_digest": digest, "carrier_kind": carrier["carrier_kind"], "file_name": item["file_name"], "content_size": len(raw)},
                    now=now,
                )

            if self.repo.membership_exists(version["id"], carrier["id"]):
                # 同一摘要重复上传：返回原成员记录，不产生新版本、不改变提交顺序
                duplicated += 1
                results.append(
                    {
                        "carrier_id": carrier["id"], "content_digest": digest,
                        "file_name": item["file_name"], "status": "duplicate",
                        "message": "内容摘要已存在，返回原记录",
                    }
                )
                continue

            sequence_no = self.repo.next_sequence(version["id"])
            self.repo.add_membership(
                version_id=version["id"], carrier_id=carrier["id"], batch_id=batch["id"],
                sequence_no=sequence_no, file_name=item["file_name"], inclusion_type="received",
                submitted_by=principal.user_id, now=now,
            )
            created += 1
            self.repo.append_event(
                project_id=project["id"], version_id=version["id"], carrier_id=carrier["id"],
                event_type="carrier.added", actor_user_id=principal.user_id,
                details={"sequence_no": sequence_no, "batch_seq": batch["batch_seq"], "file_name": item["file_name"], "content_digest": digest},
                now=now,
            )
            results.append(
                {
                    "carrier_id": carrier["id"], "content_digest": digest,
                    "file_name": item["file_name"], "sequence_no": sequence_no, "status": "added",
                }
            )

        self.repo.touch_project(project["id"], now)
        self.audit.record(
            principal, "disclosure_package.carriers_submitted", "disclosure_version", str(version["id"]),
            after={
                "version_no": version["version_no"],
                "batch_seq": batch["batch_seq"] if batch else None,
                "added": created, "duplicated": duplicated,
            },
            metadata={"project_code": project_code, "inherited_from": inherited_from},
        )
        return {
            "project": {"id": project["id"], "project_code": project["project_code"]},
            "version": {"id": version["id"], "version_no": version["version_no"], "status": version["status"]},
            "batch": (
                {"id": batch["id"], "batch_seq": batch["batch_seq"], "label": batch["label"]}
                if batch else None
            ),
            "inherited_from_version_no": inherited_from,
            "added": created,
            "duplicated": duplicated,
            "carriers": results,
        }

    def _route_submission(
        self,
        project: dict[str, Any],
        label: str,
        decoded: list[tuple[dict[str, Any], bytes, str, dict[str, Any] | None]],
        user_id: int,
        now: str,
    ) -> tuple[dict[str, Any], dict[str, Any] | None, int | None]:
        """选择载体归属的版本与批次。

        草稿期补件进入同版本新批次；已送审冻结时，仅当确有新材料才派生后续版本
        （先按原顺序继承冻结快照），纯重复上传返回冻结版本原记录、不产生新版本。
        """
        latest = self.repo.latest_version(project["id"])

        def is_new_to_latest(existing: dict[str, Any] | None) -> bool:
            return existing is None or not self.repo.membership_exists(latest["id"], existing["id"])

        if latest is None:
            version = self.repo.create_version(project["id"], 1, user_id, now)
            self.repo.append_event(
                project_id=project["id"], version_id=version["id"], carrier_id=None,
                event_type="version.created", actor_user_id=user_id, details={"version_no": 1}, now=now,
            )
            batch = self._create_batch(version, label, user_id, now)
            return version, batch, None

        has_new_material = any(is_new_to_latest(existing) for _, _, _, existing in decoded)

        if latest["status"] == "draft":
            if not has_new_material:
                return latest, None, None
            return latest, self._create_batch(latest, label, user_id, now), None

        # 最新版本已送审冻结
        if not has_new_material:
            return latest, None, None

        # 新材料形成后续版本：先按原顺序继承冻结清单，再收录新材料
        new_no = latest["version_no"] + 1
        version = self.repo.create_version(project["id"], new_no, user_id, now, based_on_version_id=latest["id"])
        snapshot_batch = self.repo.create_batch(
            version["id"], 1, SNAPSHOT_BATCH_LABEL_TEMPLATE.format(base_no=latest["version_no"]), "", user_id, now,
        )
        copied = self.repo.copy_snapshot(latest["id"], version["id"], snapshot_batch["id"], user_id, now)
        self.repo.append_event(
            project_id=project["id"], version_id=version["id"], carrier_id=None,
            event_type="version.created", actor_user_id=user_id,
            details={"version_no": new_no, "based_on_version_no": latest["version_no"], "snapshot_carriers": copied},
            now=now,
        )
        batch = self._create_batch(version, label, user_id, now)
        return version, batch, latest["version_no"]

    def _create_batch(self, version: dict[str, Any], label: str, user_id: int, now: str) -> dict[str, Any]:
        batch_seq = self.repo.next_batch_seq(version["id"])
        batch = self.repo.create_batch(version["id"], batch_seq, label, "", user_id, now)
        self.repo.append_event(
            project_id=version["project_id"], version_id=version["id"], carrier_id=None,
            event_type="batch.received", actor_user_id=user_id,
            details={"batch_seq": batch_seq, "label": label}, now=now,
        )
        return batch

    # ---- 送审冻结 ----

    def submit_version(self, principal: Principal, project_code: str, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("disclosure_packages.submit")
        project = self._require_project_by_code(project_code)
        version = self.repo.latest_version(project["id"])
        if version is None:
            raise ValidationError("交底包尚无任何载体，不能送审")
        if version["status"] == "submitted":
            raise ConflictError(f"版本 v{version['version_no']} 已经送审冻结，新材料请形成后续版本")
        memberships = self.repo.list_memberships(version["id"])
        if not memberships:
            raise ValidationError("版本不包含任何载体，不能送审")
        manifest = self._build_manifest(memberships)
        digest = hashlib.sha256(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        now = to_storage(self.clock.now())
        frozen = self.repo.freeze_version(version["id"], manifest, digest, principal.user_id, now)
        self.repo.append_event(
            project_id=project["id"], version_id=version["id"], carrier_id=None,
            event_type="version.submitted", actor_user_id=principal.user_id,
            details={"version_no": version["version_no"], "manifest_digest": digest, "carrier_count": len(manifest), "note": data.get("note", "")},
            now=now,
        )
        self.repo.touch_project(project["id"], now)
        self.audit.record(
            principal, "disclosure_package.version_submitted", "disclosure_version", str(version["id"]),
            after={"version_no": version["version_no"], "manifest_digest": digest, "carrier_count": len(manifest)},
        )
        return {
            "version": self._present_version(frozen),
            "manifest_digest": digest,
            "carrier_count": len(manifest),
        }

    @staticmethod
    def _build_manifest(memberships: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "sequence_no": item["sequence_no"],
                "file_name": item["file_name"],
                "carrier_kind": item["carrier_kind"],
                "content_digest": item["content_digest"],
                "digest_algorithm": item["digest_algorithm"],
                "inclusion_type": item["inclusion_type"],
                "batch_seq": item["batch_seq"],
            }
            for item in sorted(memberships, key=lambda item: item["sequence_no"])
        ]

    # ---- 查询 ----

    def list_versions(self, principal: Principal, project_code: str) -> dict[str, Any]:
        principal.require("disclosure_packages.read")
        project = self._require_project_by_code(project_code)
        return {"project": project, "versions": self.repo.list_versions(project["id"])}

    def version_detail(self, principal: Principal, project_code: str, version_no: int) -> dict[str, Any]:
        principal.require("disclosure_packages.read")
        project = self._require_project_by_code(project_code)
        version = self.repo.get_version_by_no(project["id"], version_no)
        memberships = self.repo.list_memberships(version["id"])
        return {
            "project": {"id": project["id"], "project_code": project["project_code"], "title": project["title"]},
            "version": self._present_version(version),
            "batches": self.repo.list_batches(version["id"]),
            "carrier_count": len(memberships),
            "carriers": memberships,
        }

    def project_events(self, principal: Principal, project_code: str) -> dict[str, Any]:
        principal.require("disclosure_packages.read")
        project = self._require_project_by_code(project_code)
        return {"project": project, "events": self.repo.list_events(project["id"])}

    def verify_carrier(self, principal: Principal, carrier_id: int) -> dict[str, Any]:
        """用存储内容重新计算摘要，复核与登记摘要是否一致。"""
        principal.require("disclosure_packages.read")
        carrier = self.repo.get_carrier(carrier_id)
        recomputed = content_digest(carrier["content_blob"])
        return {
            "carrier_id": carrier["id"],
            "content_digest": carrier["content_digest"],
            "digest_algorithm": carrier["digest_algorithm"],
            "recomputed_digest": recomputed,
            "content_size": carrier["content_size"],
            "verified": recomputed == carrier["content_digest"],
        }

    @staticmethod
    def _present_version(version: dict[str, Any]) -> dict[str, Any]:
        result = dict(version)
        if result.get("manifest_json"):
            result["manifest"] = json.loads(result["manifest_json"])
        return result

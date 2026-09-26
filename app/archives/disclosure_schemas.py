from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

CarrierKind = Literal["patent_disclosure", "laboratory_record", "design_drawing", "technical_document", "source_media", "other"]


class InventionProjectCreate(BaseModel):
    project_code: str = Field(min_length=2, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    owner_department: str = Field(min_length=1, max_length=100)


class CarrierInput(BaseModel):
    carrier_kind: CarrierKind
    file_name: str = Field(min_length=1, max_length=255)
    media_type: str = Field(default="", max_length=100)
    size_bytes: int = Field(default=0, ge=0, le=10_000_000_000)
    # 调用方先对载体原始内容计算 SHA-256；服务端只登记可复核摘要，不保存文件本体。
    content_digest: str = Field(min_length=16, max_length=128, pattern=r"^[A-Fa-f0-9]+$")


class CarriersSubmit(BaseModel):
    batch_code: str = Field(min_length=1, max_length=64)
    carriers: list[CarrierInput] = Field(min_length=1, max_length=200)

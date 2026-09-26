from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

CarrierKind = Literal["专利交底书", "实验记录", "图纸", "其他"]


class DisclosureProjectCreate(BaseModel):
    project_code: str = Field(min_length=2, max_length=64)
    title: str = Field(min_length=2, max_length=200)


class CarrierInput(BaseModel):
    file_name: str = Field(min_length=1, max_length=255)
    carrier_kind: CarrierKind = "其他"
    media_type: str = Field(default="", max_length=100)
    # 内容以 base64 传送，服务端据此计算可复核摘要，文件名变化不影响载体身份
    content_base64: str = Field(min_length=1)


class PackageSubmissionCreate(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    note: str = Field(default="", max_length=1000)
    carriers: list[CarrierInput] = Field(min_length=1, max_length=200)


class PackageSubmit(BaseModel):
    note: str = Field(default="", max_length=1000)

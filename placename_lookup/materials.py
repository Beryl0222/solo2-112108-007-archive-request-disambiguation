"""业务资料的数据结构与读取。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from read_materials import read_collection


def parse_time(value):
    """解析带时区的 ISO 8601 时间；审批与请求按同一时区比较。"""
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError(f"时间缺少时区：{value!r}")
    return moment


@dataclass(frozen=True)
class Scope:
    """卷[:页[:段]] 三级档案位置，缺省的级别表示覆盖整级。"""

    vol: str
    page: str | None = None
    segment: str | None = None

    @classmethod
    def parse(cls, text):
        parts = str(text).split(":")
        if not 1 <= len(parts) <= 3 or not parts[0]:
            raise ValueError(f"无法识别的档案位置：{text!r}")
        parts += [None] * (3 - len(parts))
        return cls(parts[0], parts[1], parts[2])

    def covers(self, other):
        """本位置是否为 other 的上级（或相同）范围。"""
        if self.vol != other.vol:
            return False
        if self.page is not None and self.page != other.page:
            return False
        if self.segment is not None and self.segment != other.segment:
            return False
        return True

    def __str__(self):
        parts = [self.vol]
        if self.page is not None:
            parts.append(self.page)
        if self.segment is not None:
            parts.append(self.segment)
        return ":".join(parts)


@dataclass(frozen=True)
class Segment:
    id: str
    visibility: str
    text: str


@dataclass(frozen=True)
class Page:
    vol: str
    page: str
    revision: int
    segments: tuple


@dataclass(frozen=True)
class GazetteerEntry:
    id: str
    place: str
    name: str
    district: str
    landmarks: tuple
    valid: tuple
    relation: str
    evidence: Scope


@dataclass(frozen=True)
class Permission:
    id: str
    reader: str
    purpose: str
    scope: Scope
    starts_at: datetime
    expires_at: datetime
    mask_terms: tuple

    def active_at(self, moment):
        """审批有效期自 starts_at 起、至 expires_at 止（不含止时刻）。"""
        return self.starts_at <= moment < self.expires_at


@dataclass(frozen=True)
class Request:
    id: str
    reader: str
    purpose: str
    at: datetime
    previous: str | None
    text: str


class Materials:
    """四类业务资料，以及按卷页取最新转录版本的索引。"""

    def __init__(self, gazetteer, pages, permissions, requests):
        self.gazetteer = list(gazetteer)
        self.pages = list(pages)
        self.permissions = list(permissions)
        self.requests = list(requests)
        self._latest = {}
        for page in self.pages:
            key = (page.vol, page.page)
            if key not in self._latest or page.revision > self._latest[key].revision:
                self._latest[key] = page

    @classmethod
    def load(cls, directory):
        directory = Path(directory)
        gazetteer = [
            GazetteerEntry(
                row["id"], row["place"], row["name"], row["district"],
                tuple(row["landmarks"]), tuple(row["valid"]), row["relation"],
                Scope.parse(row["evidence"]),
            )
            for row in read_collection(directory, "gazetteer")
        ]
        pages = [
            Page(
                row["vol"], str(row["page"]), row["revision"],
                tuple(
                    Segment(item["id"], item["visibility"], item["text"])
                    for item in row["segments"]
                ),
            )
            for row in read_collection(directory, "pages")
        ]
        permissions = [
            Permission(
                row["id"], row["reader"], row["purpose"], Scope.parse(row["scope"]),
                parse_time(row["starts_at"]), parse_time(row["expires_at"]),
                tuple(row["mask_terms"]),
            )
            for row in read_collection(directory, "permissions")
        ]
        requests = [
            Request(
                row["id"], row["reader"], row["purpose"], parse_time(row["at"]),
                row["previous"], row["text"],
            )
            for row in read_collection(directory, "requests")
        ]
        return cls(gazetteer, pages, permissions, requests)

    def latest_page(self, vol, page):
        """该卷页现行有效的转录版本（revision 最大者）。"""
        return self._latest.get((vol, str(page)))

    def find_segment(self, scope):
        """按位置取最新版本页面与段落；缺页或缺段时对应返回 None。"""
        page = self.latest_page(scope.vol, scope.page)
        if page is None:
            return None, None
        segment = next((item for item in page.segments if item.id == scope.segment), None)
        return page, segment

    def gazetteer_by_id(self):
        return {entry.id: entry for entry in self.gazetteer}

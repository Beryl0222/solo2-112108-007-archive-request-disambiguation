"""审批、可见性与遮盖。

受限内容不参与未获审批读者的任何输出；遮盖只隐去审批指定的词串，
保留部分若会误述或借简称暴露受限身份，则交馆员处理，不改写原件。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_SENTENCE = re.compile(r"[^。！？；]*[。！？；]?")
# 保留句中若出现已失效名称，需带有过去时标记，否则视为把过往写成当前
_PAST_MARKERS = ("当时", "曾", "旧", "原", "前", "以往", "当年", "年", "至", "暂", "改", "后")


def split_sentences(text):
    return [item for item in _SENTENCE.findall(text) if item]


@dataclass
class Access:
    visible: bool
    masks: tuple
    permission_ids: tuple


def active_permissions(permissions, reader, purpose, moment):
    """某读者在某用途下、某时刻有效的审批。"""
    return [
        item for item in permissions
        if item.reader == reader and item.purpose == purpose and item.active_at(moment)
    ]


def segment_access(segment, scope, permissions):
    """段落的可见性。permissions 须已按读者、用途、时间过滤。

    受限段落仅在审批范围内可见，并按审批遮盖；公开段落对所有人可见，
    但仍按该读者既有审批中仍需遮盖的范围交付。
    """
    if segment.visibility == "restricted":
        covering = [item for item in permissions if item.scope.covers(scope)]
        if not covering:
            return Access(False, (), ())
        masks = sorted({term for item in covering for term in item.mask_terms})
        return Access(True, tuple(masks), tuple(item.id for item in covering))
    masks = sorted({
        term for item in permissions if item.scope.covers(scope) for term in item.mask_terms
    })
    return Access(True, tuple(masks), ())


@dataclass
class Redaction:
    status: str          # "ok" 原文 | "masked" 已遮盖 | "librarian" 需馆员处理
    text: str | None
    reason: str | None = None


def abbreviation_leak(kept, mask_terms):
    """保留句中是否借简称再次暴露受限身份（遮盖词的二字以上片段）。"""
    for term in mask_terms:
        for size in range(2, len(term)):
            for start in range(len(term) - size + 1):
                piece = term[start:start + size]
                if piece in kept:
                    return piece
    return None


def past_as_current(kept, entries, current_year):
    """保留句是否把已失效的名称写成当前用法（缺少过去时标记）。"""
    for sentence in split_sentences(kept):
        for entry in entries:
            if entry.valid[1] < current_year and entry.name in sentence:
                if not any(marker in sentence for marker in _PAST_MARKERS):
                    return entry.name
    return None


def redact(text, mask_terms, entries, current_year):
    """按审批遮盖。

    只有一处需要遮盖时，保留能单独读懂的公开部分；遮盖点分散、遮盖后
    没有可独立读懂的公开部分、保留句把过往名称写成当前地点或借简称
    再次暴露受限身份时，交馆员处理，不悄悄改写原件。
    """
    present = [term for term in mask_terms if term in text]
    if not present:
        return Redaction("ok", text)
    sentences = split_sentences(text)
    sites = [i for i, sentence in enumerate(sentences) if any(t in sentence for t in present)]
    if len(sites) > 1:
        return Redaction("librarian", None, "遮盖点分散在多处，需馆员处理")
    kept = "".join(sentence for i, sentence in enumerate(sentences) if i != sites[0])
    if not kept.strip():
        return Redaction("librarian", None, "遮盖后没有能单独读懂的公开部分，需馆员处理")
    leak = abbreviation_leak(kept, present)
    if leak:
        return Redaction("librarian", None, f"保留部分借简称“{leak}”再次暴露受限身份，需馆员处理")
    stale = past_as_current(kept, entries, current_year)
    if stale:
        return Redaction("librarian", None, f"保留部分把过往名称“{stale}”写成当前地点，需馆员处理")
    return Redaction("masked", kept)

"""从来访文字解析名称、地标与年代线索。

解析只依据来访人自己写下的文字；名称与地标词表仅作比对用，
能否作为候选理由引用，由服务层按当次可见资料另行判定。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_CN_DIGIT = {
    "〇": 0, "零": 0, "○": 0,
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}
_PREFIXES = "旧新老"
_ERA_WIDE = ("前后", "左右", "附近", "那几年")
_NEGATION = ("不是", "并非", "勿是")
_NEGATION_WINDOW = 6
_ARABIC_YEAR = re.compile(r"(\d{4})\s*年")
_CN_YEAR = re.compile(r"([〇零○一二三四五六七八九])([〇零○一二三四五六七八九])\s*年")


def _fold(text):
    """比较用折叠：‘场/厂’互通。"""
    return text.replace("场", "厂")


def landmark_variants(landmark):
    """地标的可比写法：本体、去旧/新/老前缀、三字以上去首字的简称。"""
    variants = {landmark}
    core = landmark.lstrip(_PREFIXES)
    if core:
        variants.add(core)
        if len(core) >= 3:
            variants.add(core[1:])
    return {_fold(item) for item in variants if len(item) >= 2}


@dataclass
class Clues:
    """一次来访链条累计的线索。"""

    names: list = field(default_factory=list)            # 提到的地名
    landmarks: list = field(default_factory=list)        # (登记地标, 命中写法)
    negated_landmarks: list = field(default_factory=list)
    era: tuple | None = None                             # 年代闭区间

    def merge(self, other):
        for name in other.names:
            if name not in self.names:
                self.names.append(name)
        have = {landmark for landmark, _ in self.landmarks}
        for item in other.landmarks:
            if item[0] not in have:
                self.landmarks.append(item)
                have.add(item[0])
        for landmark in other.negated_landmarks:
            if landmark not in self.negated_landmarks:
                self.negated_landmarks.append(landmark)
        if other.era is not None:
            self.era = other.era

    @classmethod
    def from_dict(cls, data):
        return cls(
            list(data["names"]),
            [(landmark, landmark) for landmark in data["landmarks"]],
            list(data["negated_landmarks"]),
            tuple(data["era"]) if data["era"] else None,
        )


def parse_names(text, vocab):
    """最长优先扫描，避免‘新桥路’被重复记作‘新桥’。"""
    ordered = sorted(vocab, key=len, reverse=True)
    found = []
    index = 0
    while index < len(text):
        for name in ordered:
            if text.startswith(name, index):
                if name not in found:
                    found.append(name)
                index += len(name)
                break
        else:
            index += 1
    return found


def parse_era(text):
    """阿拉伯数字年份与中文两位年份（按二十世纪计）；‘前后’等放宽三年。"""
    years = [int(item) for item in _ARABIC_YEAR.findall(text)]
    for match in _CN_YEAR.finditer(text):
        years.append(1900 + 10 * _CN_DIGIT[match.group(1)] + _CN_DIGIT[match.group(2)])
    if not years:
        return None
    low, high = min(years), max(years)
    if any(word in text for word in _ERA_WIDE):
        low, high = low - 3, high + 3
    return (low, high)


def parse_clues(text, name_vocab, landmark_vocab):
    folded = _fold(text)
    landmarks = []
    for landmark in landmark_vocab:
        for variant in landmark_variants(landmark):
            if variant in folded:
                landmarks.append((landmark, variant))
                break
    negated = []
    for word in _NEGATION:
        start = 0
        while True:
            index = text.find(word, start)
            if index < 0:
                break
            window = _fold(text[index + len(word):index + len(word) + _NEGATION_WINDOW])
            for landmark in landmark_vocab:
                if any(variant in window for variant in landmark_variants(landmark)):
                    negated.append(landmark)
            start = index + len(word)
    return Clues(
        parse_names(text, name_vocab),
        landmarks,
        sorted(set(negated)),
        parse_era(text),
    )

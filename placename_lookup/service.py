"""查阅窗口服务：辨读请求、候选解释、限域摘录、会话记录与复核。

输出分两层：返回结果中下划线开头的字段仅供馆内复核，读者视图
（reader_view）不含这些字段；受限资料不参与未获审批读者的候选、
数量、顺序与追问。
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

from .access import Access, active_permissions, redact, segment_access
from .clues import Clues, parse_clues
from .materials import Request, Scope

_TRANSFER_HINTS = ("旧段", "新段", "东延", "西延", "南延", "北延", "延伸", "更名", "改段", "转移", "并入", "拆分")
_REISSUE_HINTS = ("刚才", "那份", "同上", "照旧", "再抄", "再要")
_ORDINALS = "①②③④⑤⑥⑦⑧⑨"


def reader_view(result):
    """读者可见的结果：隐去只供馆内复核的内部字段。"""
    return {key: value for key, value in result.items() if not key.startswith("_")}


def _overlap(valid, era):
    return valid[0] <= era[1] and era[0] <= valid[1]


def _intervals_linked(intervals):
    ordered = sorted(intervals)
    return all(
        later[0] <= earlier[1] + 1
        for later, earlier in zip(ordered[1:], ordered)
    )


def _merged_ranges(entries):
    """同一地点同名条目的适用期合并后按时间列出。"""
    by_name = {}
    for entry in entries:
        by_name.setdefault(entry.name, []).append(tuple(entry.valid))
    merged = []
    for name, intervals in by_name.items():
        intervals.sort()
        low, high = intervals[0]
        for start, end in intervals[1:]:
            if start <= high + 1:
                high = max(high, end)
            else:
                merged.append((name, low, high))
                low, high = start, end
        merged.append((name, low, high))
    return sorted(merged, key=lambda item: item[1])


class LookupService:
    """一个查阅窗口会话：处理来访链条、发放摘录并记录审批引用。"""

    def __init__(self, materials, log_path=None, current_year=None):
        self.materials = materials
        self.log_path = Path(log_path) if log_path else None
        self.current_year = current_year or date.today().year
        self._names = sorted({entry.name for entry in materials.gazetteer})
        self._landmarks = sorted({lm for entry in materials.gazetteer for lm in entry.landmarks})
        self._results = {}
        self._excerpts = {}
        if self.log_path and self.log_path.exists():
            self._load_log()

    # ---------- 会话记录 ----------

    def _load_log(self):
        with self.log_path.open(encoding="utf-8") as source:
            for line in source:
                entry = json.loads(line)
                self._results[entry["request_id"]] = entry
                for excerpt in entry.get("_excerpts", []):
                    self._excerpts[excerpt["excerpt_id"]] = excerpt

    def _append_log(self, result):
        self._results[result["request_id"]] = result
        for excerpt in result.get("_excerpts", []):
            self._excerpts[excerpt["excerpt_id"]] = excerpt
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as sink:
                sink.write(json.dumps(result, ensure_ascii=False) + "\n")

    def _chain_excerpts(self, request_id):
        """来访链条上已发放的全部摘录（按链条顺序去重）。"""
        excerpts, seen = [], set()
        current = self._results.get(request_id)
        while current:
            for excerpt in current.get("_excerpts", []):
                if excerpt["excerpt_id"] not in seen:
                    seen.add(excerpt["excerpt_id"])
                    excerpts.append(excerpt)
            current = self._results.get(current.get("previous"))
        return excerpts

    # ---------- 可见性 ----------

    def _permissions_for(self, reader, purpose, moment):
        return active_permissions(self.materials.permissions, reader, purpose, moment)

    def _segment_access(self, scope, reader, purpose, moment):
        _, segment = self.materials.find_segment(scope)
        if segment is None:
            # 无逐页录文的依据按公开目录处理；有页无段则不可见
            visible = self.materials.latest_page(scope.vol, scope.page) is None
            return None, Access(visible, (), ())
        return segment, segment_access(
            segment, scope, self._permissions_for(reader, purpose, moment)
        )

    def visible_entries(self, reader, purpose, moment):
        """该身份此时可依据的地名条目：依据段落公开或已获审批。"""
        entries = []
        for entry in self.materials.gazetteer:
            _, access = self._segment_access(entry.evidence, reader, purpose, moment)
            if access.visible:
                entries.append(entry)
        return entries

    # ---------- 候选与分类 ----------

    def build_candidates(self, clues, entries):
        """按线索为各登记地点评分；候选理由只引用可见条目。"""
        by_place = {}
        for entry in entries:
            by_place.setdefault(entry.place, []).append(entry)
        landmark_names = {landmark for landmark, _ in clues.landmarks}
        negated = set(clues.negated_landmarks)
        candidates = []
        for place in sorted(by_place):
            ents = by_place[place]
            name_hits = [e for e in ents if e.name in clues.names]
            if clues.names and not name_hits:
                continue
            landmark_hits = {}
            for entry in ents:
                hits = (set(entry.landmarks) & landmark_names) - negated
                if hits:
                    landmark_hits[entry.id] = sorted(hits)
            negated_hits = sorted({lm for e in ents for lm in set(e.landmarks) & negated})
            era_hits = [e for e in name_hits if clues.era and _overlap(e.valid, clues.era)]
            consistent = bool(name_hits) if clues.names else bool(landmark_hits)
            if clues.era:
                consistent = consistent and bool(era_hits)
            if negated_hits:
                consistent = False
            score = 0
            if name_hits:
                score += 3
            if era_hits:
                score += 2
            score += len({lm for hits in landmark_hits.values() for lm in hits})
            if negated_hits:
                score -= 3
            reasons = []
            for entry in sorted(name_hits, key=lambda e: (e.valid[0], e.id)):
                reasons.append(
                    f"名称‘{entry.name}’（{entry.valid[0]}–{entry.valid[1]} 年，"
                    f"{entry.relation}，依据 {entry.evidence}，条目 {entry.id}）"
                )
            for entry_id, hits in sorted(landmark_hits.items()):
                reasons.append(f"地标 {'、'.join(hits)} 与来访描述相符（条目 {entry_id}）")
            if clues.era and era_hits:
                joined = "、".join(entry.id for entry in era_hits)
                reasons.append(f"年代 {clues.era[0]}–{clues.era[1]} 与名称适用期相合（条目 {joined}）")
            if negated_hits:
                reasons.append(f"来访人排除地标 {'、'.join(negated_hits)}")
            cited = {e.id for e in name_hits} | set(landmark_hits) | {e.id for e in era_hits}
            candidates.append({
                "place": place,
                "district": ents[0].district,
                "score": score,
                "consistent": consistent,
                "reasons": reasons,
                "entry_ids": sorted(cited),
            })
        candidates.sort(key=lambda item: (-item["score"], item["place"]))
        return candidates

    def classify(self, entries):
        """区分同址改名、名称转移与两个地方碰巧同名。"""
        relations = []
        by_place = {}
        for entry in entries:
            by_place.setdefault(entry.place, []).append(entry)
        for place in sorted(by_place):
            names = []
            for entry in sorted(by_place[place], key=lambda e: (e.valid[0], e.id)):
                if entry.name not in [name for name, _ in names]:
                    names.append((entry.name, entry.valid))
            if len(names) > 1:
                chain = " → ".join(f"{name}（{valid[0]}–{valid[1]} 年）" for name, valid in names)
                relations.append({
                    "type": "同址改名",
                    "place": place,
                    "chain": chain,
                    "entry_ids": sorted(e.id for e in by_place[place]),
                })
        by_name = {}
        for entry in entries:
            by_name.setdefault(entry.name, []).append(entry)
        for name in sorted(by_name):
            ents = by_name[name]
            places = sorted({e.place for e in ents})
            if len(places) < 2:
                continue
            rels = {e.relation for e in ents}
            districts = {e.district for e in ents}
            if any("同名" in relation for relation in rels):
                kind = "同名异地"
            elif any(hint in relation for relation in rels for hint in _TRANSFER_HINTS):
                kind = "名称转移"
            elif len(districts) == 1 and _intervals_linked([e.valid for e in ents]):
                kind = "名称转移"
            else:
                kind = "同名异地"
            relations.append({
                "type": kind,
                "name": name,
                "places": places,
                "entry_ids": sorted(e.id for e in ents),
            })
        return relations

    # ---------- 请求处理 ----------

    def handle_request(self, request):
        """处理一条来访记录：辨读线索、追问或发放限域摘录，并写入会话记录。"""
        prior = self._results.get(request.previous) if request.previous else None
        clues = Clues.from_dict(prior["clues"]) if prior else Clues()
        clues.merge(parse_clues(request.text, self._names, self._landmarks))
        notices = []
        if prior and prior["purpose"] != request.purpose:
            notices.append(
                f"用途由‘{prior['purpose']}’变为‘{request.purpose}’；"
                "先前按原用途发放的摘录编号不能成为新用途的查阅许可。"
            )
            for excerpt in self._chain_excerpts(request.previous):
                notices.append(
                    f"摘录 {excerpt['excerpt_id']}（{excerpt['scope']}，"
                    f"用途‘{excerpt['purpose']}’）不适用于本次用途。"
                )
        entries = self.visible_entries(request.reader, request.purpose, request.at)
        if prior and any(hint in request.text for hint in _REISSUE_HINTS):
            result = self._reissue(request, prior, clues, entries)
        else:
            result = self._lookup(request, prior, clues, entries)
        result["notices"] = notices + result["notices"]
        self._append_log(result)
        return result

    def _lookup(self, request, prior, clues, entries):
        candidates = self.build_candidates(clues, entries)
        relations = self.classify(entries)
        result = self._base_result(request, clues, entries, candidates, relations)
        if not clues.names and not clues.landmarks and clues.era is None:
            result["status"] = "no_match"
            result["next_steps"] = ["请提供地名、附近地标或大致年代。"]
            return result
        selected = self._detect_selection(request.text, prior)
        consistent = [item for item in candidates if item["consistent"]]
        if selected is not None:
            chosen = [item for item in candidates if item["place"] == selected]
            if chosen:
                consistent = chosen
                result["notices"].append(f"已按来访人选择的地点 {selected} 处理。")
        if len(consistent) > 1:
            result["status"] = "needs_clarification"
            result["question"] = self._build_question(consistent, clues, entries)
            result["next_steps"] = ["请按上述问题补充一项信息，窗口再确定可调卷的页或段落。"]
        elif len(consistent) == 1:
            place = consistent[0]
            result["status"] = "resolved"
            result["resolution"] = {
                "place": place["place"],
                "district": place["district"],
                "basis": place["reasons"],
            }
            deliverables, revisions, perms, excerpts, notes, delivered_ids = self._deliver(
                place["place"], request, entries
            )
            result["deliverables"] = deliverables
            result["_revisions_used"] = revisions
            result["_permissions_used"] = perms
            result["_excerpts"] = excerpts
            result["_internal_notes"] = notes
            result["_evidence_used"] = sorted(set(result["_evidence_used"]) | set(delivered_ids))
            steps = []
            if any(item["status"] == "需馆员处理" for item in deliverables):
                steps.append("标注‘需馆员处理’的摘录请交馆员处理后再交付，窗口不改写原件。")
            if perms:
                expiry = min(
                    item.expires_at for item in self.materials.permissions if item.id in perms
                )
                result["notices"].append(
                    f"本次摘录依据审批 {'、'.join(perms)}，有效期至 {expiry.isoformat()}；"
                    "逾期或变更用途需重新核验。"
                )
                steps.append("审批到期或用途变化时，请重新申请后再查阅。")
            else:
                steps.append("以上为公开依据的摘录；如需调阅其他未出示页面，请向窗口申请审批。")
            result["next_steps"] = steps
        else:
            result["status"] = "no_match"
            if candidates:
                result["question"] = self._build_question(candidates, clues, entries)
                result["next_steps"] = ["现有线索与可出示依据不完全相合，请确认年代或片区。"]
            else:
                result["next_steps"] = ["可补充大致年代、所在片区或附近地标后再查。"]
        return result

    def _reissue(self, request, prior, clues, entries):
        result = self._base_result(request, clues, entries, [], [])
        outcomes, steps = [], []
        for excerpt in prior.get("_excerpts", []):
            outcome = self.reissue_excerpt(
                excerpt["excerpt_id"], request.reader, request.purpose, request.at
            )
            outcomes.append(outcome)
            if outcome["status"] == "refused" and outcome.get("next_step"):
                steps.append(f"{outcome['next_step']}（{outcome['scope']}）")
        delivered = [item for item in outcomes if item["status"] == "ok"]
        if delivered:
            scopes = "、".join(item["scope"] for item in delivered)
            steps.append(f"公开或仍在审批有效期内的段落已重发：{scopes}。")
        result["status"] = "resolved" if outcomes and len(delivered) == len(outcomes) else "refused"
        result["reissues"] = outcomes
        result["next_steps"] = steps or ["可改为新的查档请求。"]
        return result

    def _base_result(self, request, clues, entries, candidates, relations):
        cited = set()
        for candidate in candidates:
            cited.update(candidate["entry_ids"])
        for relation in relations:
            cited.update(relation["entry_ids"])
        return {
            "request_id": request.id,
            "previous": request.previous,
            "reader": request.reader,
            "purpose": request.purpose,
            "at": request.at.isoformat(),
            "status": None,
            "clues": self._public_clues(clues, entries),
            "notices": [],
            "candidates": candidates,
            "relations": relations,
            "question": None,
            "resolution": None,
            "deliverables": [],
            "next_steps": [],
            "_evidence_used": sorted(cited),
            "_revisions_used": {},
            "_permissions_used": [],
            "_excerpts": [],
            "_internal_notes": [],
        }

    def _public_clues(self, clues, entries):
        """线索回显只保留当次可见条目中出现过的登记地标。"""
        visible_landmarks = {lm for entry in entries for lm in entry.landmarks}
        return {
            "names": list(clues.names),
            "landmarks": [lm for lm, _ in clues.landmarks if lm in visible_landmarks],
            "negated_landmarks": [lm for lm in clues.negated_landmarks if lm in visible_landmarks],
            "era": list(clues.era) if clues.era else None,
            "unmatched_names": [
                name for name in clues.names if not any(e.name == name for e in entries)
            ],
        }

    def _detect_selection(self, text, prior):
        """来访人从上次已显示的候选中选定地点（片区名、地点编号或序号）。"""
        if not prior:
            return None
        candidates = prior.get("candidates") or []
        hits = [c["place"] for c in candidates if c["district"] and c["district"] in text]
        hits += [c["place"] for c in candidates if c["place"] in text]
        for index, candidate in enumerate(candidates):
            if index < len(_ORDINALS) and _ORDINALS[index] in text:
                hits.append(candidate["place"])
        unique = sorted(set(hits))
        return unique[0] if len(unique) == 1 else None

    def _build_question(self, consistent, clues, entries):
        """一个能实质排除歧义的问题，只依据可见资料。"""
        matched = [name for name in clues.names if any(e.name == name for e in entries)]
        label = "、".join(f"‘{name}’" for name in matched) or "该地名"
        lines = [f"{label}在可出示的依据中对应 {len(consistent)} 处地点："]
        vols = set()
        for index, candidate in enumerate(consistent):
            ents = [e for e in entries if e.place == candidate["place"]]
            landmarks = "、".join(sorted({lm for e in ents for lm in e.landmarks})) or "无登记地标"
            ranges = "；".join(
                f"{low}–{high} 年称‘{name}’" for name, low, high in _merged_ranges(ents)
            )
            lines.append(f"{_ORDINALS[index]} {candidate['district']}（地标：{landmarks}；{ranges}）")
            vols.update(e.evidence.vol for e in ents)
        lines.append(f"涉及卷 {'、'.join(sorted(vols))}，调卷与审批范围不同。")
        districts = "／".join(candidate["district"] for candidate in consistent)
        lines.append(f"请补充一项即可定位：大致年代，或所在片区（{districts}）？")
        return "\n".join(lines)

    # ---------- 摘录 ----------

    def _deliver(self, place, request, entries):
        """确定该用途下可阅的页或段落，按审批遮盖后发放摘录。"""
        deliverables, excerpts, notes, delivered_ids = [], [], [], []
        revisions, permissions = {}, set()
        targets = [e for e in entries if e.place == place]
        for entry in sorted(targets, key=lambda e: str(e.evidence)):
            scope = entry.evidence
            page = self.materials.latest_page(scope.vol, scope.page)
            if page is None:
                deliverables.append({
                    "scope": str(scope), "status": "目录依据", "note": "公开目录条目，无逐页录文。",
                })
                delivered_ids.append(entry.id)
                continue
            segment, access = self._segment_access(scope, request.reader, request.purpose, request.at)
            if segment is None or not access.visible:
                continue
            revisions[f"{scope.vol}:{scope.page}"] = page.revision
            permissions.update(access.permission_ids)
            outcome = redact(segment.text, access.masks, entries, self.current_year)
            if outcome.status == "librarian":
                notes.append(f"{scope}：{outcome.reason}")
                deliverables.append({
                    "scope": str(scope),
                    "revision": page.revision,
                    "status": "需馆员处理",
                    "note": "该摘录需馆员处理后再交付，窗口不改写原件。",
                })
                delivered_ids.append(entry.id)
                continue
            record = self._issue_excerpt(
                request.reader, request.purpose, scope, page.revision,
                outcome.text, access, outcome.status, request.at,
            )
            excerpts.append(record)
            item = {
                "scope": str(scope),
                "revision": page.revision,
                "status": outcome.status,
                "text": outcome.text,
                "excerpt_id": record["excerpt_id"],
                "permission_ids": record["permission_ids"],
            }
            if outcome.status == "masked":
                item["note"] = "本段其余部分按审批遮盖。"
            deliverables.append(item)
            delivered_ids.append(entry.id)
        return deliverables, revisions, sorted(permissions), excerpts, notes, delivered_ids

    def _issue_excerpt(self, reader, purpose, scope, revision, text, access, status, moment):
        key = "|".join([reader, purpose, str(scope), str(revision), moment.isoformat(), text])
        excerpt_id = "E-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
        record = {
            "excerpt_id": excerpt_id,
            "reader": reader,
            "purpose": purpose,
            "scope": str(scope),
            "revision": revision,
            "text": text,
            "permission_ids": list(access.permission_ids),
            "masks": list(access.masks),
            "status": status,
            "issued_at": moment.isoformat(),
        }
        self._excerpts[excerpt_id] = record
        return record

    def reissue_excerpt(self, excerpt_id, reader, purpose, moment):
        """按摘录编号复查：审批过期或用途变化时，编号不能成为新的查阅许可。"""
        record = self._excerpts.get(excerpt_id)
        if record is None:
            return {"excerpt_id": excerpt_id, "status": "refused", "reason": "无此摘录记录。"}
        outcome = {"excerpt_id": excerpt_id, "scope": record["scope"]}
        if record["reader"] != reader:
            outcome.update(status="refused", reason="摘录按原读者发放，不能转交他人。")
            return outcome
        if record["purpose"] != purpose:
            outcome.update(
                status="refused",
                reason=f"摘录按用途‘{record['purpose']}’发放，不能作为用途‘{purpose}’的查阅许可。",
                next_step="请以当前用途重新申请审批。",
            )
            return outcome
        known = {item.id: item for item in self.materials.permissions}
        expired = [
            known[pid] for pid in record["permission_ids"]
            if pid in known and not known[pid].active_at(moment)
        ]
        missing = [pid for pid in record["permission_ids"] if pid not in known]
        if missing:
            outcome.update(status="refused", reason=f"审批 {'、'.join(missing)} 已无记录。")
        elif expired:
            when = min(item.expires_at for item in expired).isoformat()
            ids = "、".join(item.id for item in expired)
            outcome.update(
                status="refused",
                reason=f"审批 {ids} 已于 {when} 到期，先前的摘录编号不能成为新的查阅许可。",
                next_step="请重新提交查阅申请",
            )
        else:
            outcome.update(
                status="ok",
                revision=record["revision"],
                text=record["text"],
                note="在审批有效期内按原摘录重发。",
            )
        return outcome

    # ---------- 复核 ----------

    def review(self, request_id, reviewer, moment):
        """有权人员复核当次用了哪版文字和地名证据。

        复核人须持有无遮盖的有效审批，且其范围覆盖当次用到的全部受限位置。
        """
        entry = self._results.get(request_id)
        if entry is None:
            return {"status": "denied", "reason": f"没有编号为 {request_id} 的查阅记录。"}
        restricted = set()
        for excerpt in entry.get("_excerpts", []):
            scope = Scope.parse(excerpt["scope"])
            _, segment = self.materials.find_segment(scope)
            if segment is not None and segment.visibility == "restricted":
                restricted.add(scope)
        by_id = self.materials.gazetteer_by_id()
        for gid in entry.get("_evidence_used", []):
            evidence = by_id.get(gid)
            if evidence is None:
                continue
            _, segment = self.materials.find_segment(evidence.evidence)
            if segment is not None and segment.visibility == "restricted":
                restricted.add(evidence.evidence)
        unmasked = [
            item for item in self.materials.permissions
            if item.reader == reviewer and not item.mask_terms and item.active_at(moment)
        ]
        if not unmasked:
            return {"status": "denied", "reason": "复核需持有无遮盖的有效审批。"}
        missing = [
            str(scope) for scope in sorted(restricted, key=str)
            if not any(item.scope.covers(scope) for item in unmasked)
        ]
        if missing:
            return {"status": "denied", "reason": f"复核权限未覆盖受限位置：{'、'.join(missing)}。"}
        return {
            "status": "ok",
            "request_id": request_id,
            "reader": entry["reader"],
            "purpose": entry["purpose"],
            "at": entry["at"],
            "evidence_used": entry.get("_evidence_used", []),
            "revisions_used": entry.get("_revisions_used", {}),
            "permissions_used": entry.get("_permissions_used", []),
            "excerpts": [
                {
                    "excerpt_id": excerpt["excerpt_id"],
                    "scope": excerpt["scope"],
                    "revision": excerpt["revision"],
                    "permission_ids": excerpt["permission_ids"],
                    "masks": excerpt["masks"],
                }
                for excerpt in entry.get("_excerpts", [])
            ],
        }

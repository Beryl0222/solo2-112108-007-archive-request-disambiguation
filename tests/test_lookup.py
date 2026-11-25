"""查阅工具的流程与规则测试。"""
import json
import tempfile
import unittest
from pathlib import Path

from placename_lookup import (
    LookupService,
    Materials,
    Request,
    Scope,
    parse_time,
    reader_view,
    redact,
)
from placename_lookup.materials import GazetteerEntry

ROOT = Path(__file__).resolve().parents[1]
AT = "2026-11-24T09:00:00+08:00"


class FlowTests(unittest.TestCase):
    """按时间顺序处理资料中的六条来访记录。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        materials = Materials.load(ROOT / "data")
        self.service = LookupService(
            materials,
            log_path=Path(self.tmp.name) / "session_log.jsonl",
            current_year=2026,
        )
        self.results = {}
        for request in sorted(materials.requests, key=lambda item: item.at):
            self.results[request.id] = self.service.handle_request(request)

    def test_x1_asks_one_question_based_on_visible_materials(self):
        result = self.results["x1"]
        self.assertEqual(result["status"], "needs_clarification")
        consistent = [c["place"] for c in result["candidates"] if c["consistent"]]
        self.assertEqual(consistent, ["br-old", "br-east"])
        question = result["question"]
        self.assertIsNotNone(question)
        self.assertEqual(question.count("？"), 1)
        self.assertIn("上坪", question)
        self.assertIn("下渡", question)
        # 追问、候选与分类不得出现受限页的内容或线索
        shown = json.dumps(
            {k: v for k, v in reader_view(result).items() if k != "clues"},
            ensure_ascii=False,
        )
        for leaked in ("过水桥", "借址", "secret", "g6", "新九号", "甲二"):
            self.assertNotIn(leaked, shown)
        types = {(r["type"], r.get("name")) for r in result["relations"]}
        self.assertIn(("同名异地", "新桥"), types)
        self.assertIn(("名称转移", "新桥路"), types)
        self.assertIn(("同址改名", None), types)

    def test_x2_resolves_with_era_and_masks_single_site(self):
        result = self.results["x2"]
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["resolution"]["place"], "br-east")
        by_scope = {d["scope"]: d for d in result["deliverables"]}
        self.assertEqual(set(by_scope), {"v2:1:a", "v2:2:secret"})
        secret = by_scope["v2:2:secret"]
        self.assertEqual(secret["status"], "masked")
        self.assertEqual(secret["text"], "当时小学在下渡借址，旧称过水桥。")
        self.assertEqual(secret["permission_ids"], ["p1"])
        self.assertNotIn("masks", secret)  # 遮盖词本身不交给读者
        self.assertEqual(by_scope["v2:1:a"]["text"], "下渡渔校东侧，港沟新桥于五六年竣工。")
        self.assertEqual(result["_revisions_used"], {"v2:1": 1, "v2:2": 1})
        self.assertTrue(any("p1" in notice for notice in result["notices"]))

    def test_x3_expired_excerpt_is_not_a_new_license(self):
        result = self.results["x3"]
        self.assertEqual(result["status"], "refused")
        by_scope = {o["scope"]: o for o in result["reissues"]}
        self.assertEqual(by_scope["v2:1:a"]["status"], "ok")
        refused = by_scope["v2:2:secret"]
        self.assertEqual(refused["status"], "refused")
        self.assertIn("p1", refused["reason"])
        self.assertIn("到期", refused["reason"])
        self.assertNotIn("text", refused)

    def test_x4_purpose_change_does_not_extend_approval(self):
        result = self.results["x4"]
        self.assertEqual(result["purpose"], "地籍")
        self.assertEqual(result["status"], "resolved")
        self.assertEqual([d["scope"] for d in result["deliverables"]], ["v2:1:a"])
        self.assertTrue(any("用途" in notice for notice in result["notices"]))
        self.assertTrue(any("不适用" in notice for notice in result["notices"]))

    def test_x5_name_transfer_and_reader_exclusion(self):
        result = self.results["x5"]
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["resolution"]["place"], "rd-west")
        self.assertEqual([d["scope"] for d in result["deliverables"]], ["v3:3:a"])
        transfer = [r for r in result["relations"] if r["type"] == "名称转移"]
        self.assertEqual(len(transfer), 1)
        self.assertEqual(transfer[0]["name"], "新桥路")
        self.assertEqual(transfer[0]["places"], ["rd-east", "rd-west"])
        rd_east = next(c for c in result["candidates"] if c["place"] == "rd-east")
        self.assertFalse(rd_east["consistent"])

    def test_x6_staff_view_uses_restricted_page(self):
        result = self.results["x6"]
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["resolution"]["place"], "br-east")
        by_scope = {d["scope"]: d for d in result["deliverables"]}
        self.assertEqual(by_scope["v2:2:secret"]["status"], "ok")
        self.assertIn("新九号", by_scope["v2:2:secret"]["text"])

    def test_review_requires_unmasked_covering_permission(self):
        ok = self.service.review("x2", "staff7", parse_time(AT))
        self.assertEqual(ok["status"], "ok")
        self.assertIn("g6", ok["evidence_used"])
        self.assertEqual(ok["revisions_used"], {"v2:1": 1, "v2:2": 1})
        self.assertEqual(ok["permissions_used"], ["p1"])
        denied = self.service.review("x2", "visitor10", parse_time(AT))
        self.assertEqual(denied["status"], "denied")

    def test_latest_revision_and_public_segment_masking(self):
        request = Request(
            "t1", "visitor10", "校史",
            parse_time("2026-11-23T10:30:00+08:00"), None, "查通学桥。",
        )
        result = self.service.handle_request(request)
        self.assertEqual(result["status"], "resolved")
        by_scope = {d["scope"]: d for d in result["deliverables"]}
        self.assertEqual(by_scope["v1:2:a"]["revision"], 2)
        self.assertEqual(by_scope["v1:2:a"]["text"], "自六七年起，前所称新桥改通学桥。")
        self.assertEqual(by_scope["v1:1:a"]["status"], "ok")

    def test_restricted_name_invisible_without_approval(self):
        request = Request(
            "t2", "visitor2", "文史",
            parse_time("2026-11-23T09:30:00+08:00"), None, "查过水桥。",
        )
        result = self.service.handle_request(request)
        self.assertEqual(result["status"], "no_match")
        self.assertEqual(result["candidates"], [])
        shown = json.dumps(
            {k: v for k, v in reader_view(result).items() if k != "clues"},
            ensure_ascii=False,
        )
        for leaked in ("过水桥", "下渡", "secret", "g6"):
            self.assertNotIn(leaked, shown)

    def test_reissue_checks_reader_and_purpose(self):
        secret = next(
            e for e in self.results["x2"]["_excerpts"] if e["scope"] == "v2:2:secret"
        )
        moment = parse_time("2026-11-23T10:30:00+08:00")
        other = self.service.reissue_excerpt(secret["excerpt_id"], "visitor2", "校史", moment)
        self.assertEqual(other["status"], "refused")
        changed = self.service.reissue_excerpt(secret["excerpt_id"], "visitor10", "地籍", moment)
        self.assertEqual(changed["status"], "refused")
        self.assertIn("用途", changed["reason"])
        ok = self.service.reissue_excerpt(secret["excerpt_id"], "visitor10", "校史", moment)
        self.assertEqual(ok["status"], "ok")


class RedactionTests(unittest.TestCase):
    def entry(self, name="过水桥", valid=(1971, 1974)):
        return GazetteerEntry(
            "g9", "p1", name, "某区", (), valid, "测试", Scope.parse("v9:9:a"),
        )

    def test_single_site_keeps_readable_public_part(self):
        outcome = redact("当时小学在下渡借址，旧称过水桥。档主住新九号，家属号甲二。",
                         ["新九号", "甲二"], [self.entry()], 2026)
        self.assertEqual(outcome.status, "masked")
        self.assertEqual(outcome.text, "当时小学在下渡借址，旧称过水桥。")

    def test_scattered_sites_require_librarian(self):
        outcome = redact("甲二在此。甲二在彼。", ["甲二"], [], 2026)
        self.assertEqual(outcome.status, "librarian")

    def test_abbreviation_leak_requires_librarian(self):
        outcome = redact("他住新九。档主住新九号。", ["新九号"], [], 2026)
        self.assertEqual(outcome.status, "librarian")
        self.assertIn("新九", outcome.reason)

    def test_past_written_as_current_requires_librarian(self):
        outcome = redact("过水桥在村口。档主住新九号。", ["新九号"], [self.entry()], 2026)
        self.assertEqual(outcome.status, "librarian")
        marked = redact("旧称过水桥。档主住新九号。", ["新九号"], [self.entry()], 2026)
        self.assertEqual(marked.status, "masked")

    def test_no_public_part_requires_librarian(self):
        outcome = redact("档主住新九号。", ["新九号"], [], 2026)
        self.assertEqual(outcome.status, "librarian")

    def test_unmasked_text_passes_through(self):
        outcome = redact("新桥在校后。", ["新九号"], [self.entry("新桥", (1936, 1966))], 2026)
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.text, "新桥在校后。")


class ScopeTests(unittest.TestCase):
    def test_covers(self):
        self.assertTrue(Scope.parse("v2").covers(Scope.parse("v2:2:secret")))
        self.assertTrue(Scope.parse("v2:2").covers(Scope.parse("v2:2:secret")))
        self.assertFalse(Scope.parse("v2:2:secret").covers(Scope.parse("v2:2")))
        self.assertFalse(Scope.parse("v1:2").covers(Scope.parse("v2:2")))

    def test_permission_window(self):
        materials = Materials.load(ROOT / "data")
        p1 = next(p for p in materials.permissions if p.id == "p1")
        self.assertFalse(p1.active_at(parse_time("2026-11-23T08:59:00+08:00")))
        self.assertTrue(p1.active_at(parse_time("2026-11-23T09:00:00+08:00")))
        self.assertFalse(p1.active_at(parse_time("2026-11-23T12:00:00+08:00")))


if __name__ == "__main__":
    unittest.main()

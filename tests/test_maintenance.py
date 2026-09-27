import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError
from src.rules import DomainRules


def voyage_data(berth="B12", eta=6, etd=18, vessel="HaiYun"):
    return {'vessel': vessel, 'berth': berth, 'vessel_length_m': 180, 'berth_length_m': 220,
            'draft_m': 10.2, 'berth_depth_m': 11.5, 'eta_hour': eta, 'etd_hour': etd,
            'risk_level': 'medium', 'dangerous_goods': False, 'dangerous_class': ''}


def maint_data(berth="B12", start=8, end=16, work="岸桥轨道更换"):
    return {'berth': berth, 'start_hour': start, 'end_hour': end, 'work': work}


CREW = Actor("repair", "maintenance_crew")
CTL = Actor("control", "port_controller")
ADMIN = Actor("boss", "admin")


class MaintenanceSchedulingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def create_voyage(self, ref, eta=6, etd=18, berth="B12", vessel="HaiYun"):
        return self.service.create(CTL, ref, voyage_data(berth, eta, etd, vessel))

    def create_maintenance(self, ref="M-1", start=8, end=16, actor=CREW, berth="B12", work="岸桥轨道更换"):
        return self.service.create_maintenance(actor, ref, maint_data(berth, start, end, work))

    def act(self, record, action, data, actor=None):
        actor = actor or (CREW if record.get("kind") == "maintenance" else CTL)
        return self.service.act(actor, record["id"], record["version"], action, data)

    def test_maintenance_lifecycle_and_permissions(self):
        m = self.create_maintenance()
        self.assertEqual(m["state"], "draft")
        self.assertEqual(m["kind"], "maintenance")
        # 调度员无权登记维修单
        with self.assertRaises(PermissionDenied):
            self.service.create_maintenance(CTL, "M-X", maint_data())
        # 维修班无权登记航次
        with self.assertRaises(PermissionDenied):
            self.service.create(CREW, "V-X", voyage_data())
        m = self.act(m, "confirm", {})
        self.assertEqual(m["state"], "confirmed")
        # 确认中的维修单再确认被状态机拒绝
        with self.assertRaises(Conflict):
            self.act(m, "confirm", {})
        m = self.act(m, "extend", {"new_end_hour": 20, "reason": "配件迟到"})
        self.assertEqual(m["state"], "extended")
        self.assertEqual(m["payload"]["end_hour"], 20)
        m = self.act(m, "complete", {"actual_end_hour": 18})
        self.assertEqual(m["state"], "completed")
        self.assertTrue(m["payload"]["completed_early"])

    def test_confirm_lock_reschedules_colliding_plan(self):
        # 计划 6-18 已正常确认（现有流程“照常确认”）
        v = self.create_voyage("VOY-1")
        v = self.service.act(CTL, v["id"], v["version"], "confirm", {"pilot_id": "P-01"})
        self.assertEqual(v["state"], "confirmed")
        # 维修班下单 8-16，确认后撞上：候泊计划顺延到 16 时
        m = self.create_maintenance("M-1", 8, 16)
        m = self.act(m, "confirm", {})
        v = self.service.get_record(CTL, v["id"])
        self.assertEqual((v["payload"]["eta_hour"], v["payload"]["etd_hour"]), (16, 28))
        self.assertEqual(v["state"], "confirmed")  # 状态照旧，只动窗口
        self.assertEqual(v["payload"]["original_eta_hour"], 6)
        self.assertEqual(v["payload"]["last_reschedule"]["trigger"], "maintenance_confirmed")
        self.assertIn("岸桥轨道更换", v["payload"]["last_reschedule"]["reason"])
        # 审计时间线可追溯重排
        timeline = self.service.timeline(CTL, v["id"])
        self.assertEqual(timeline[-1]["action"], "rescheduled")

    def test_berthed_and_departed_voyages_are_left_untouched(self):
        berthed = self.create_voyage("VOY-B", 6, 18, vessel="AtBerth")
        berthed = self.service.act(CTL, berthed["id"], berthed["version"], "confirm", {"pilot_id": "P-01"})
        berthed = self.service.act(CTL, berthed["id"], berthed["version"], "berth", {"actual_draft_m": 10.3})
        departed = self.create_voyage("VOY-D", 2, 5, vessel="Gone")
        departed = self.service.act(CTL, departed["id"], departed["version"], "confirm", {"pilot_id": "P-02"})
        departed = self.service.act(CTL, departed["id"], departed["version"], "berth", {"actual_draft_m": 10.0})
        departed = self.service.act(CTL, departed["id"], departed["version"], "depart", {"cargo_operation_complete": True})
        m = self.create_maintenance("M-1", 8, 16)
        self.act(m, "confirm", {})
        berthed2 = self.service.get_record(CTL, berthed["id"])
        departed2 = self.service.get_record(CTL, departed["id"])
        self.assertEqual((berthed2["payload"]["eta_hour"], berthed2["payload"]["etd_hour"]), (6, 18))
        self.assertEqual(berthed2["state"], "berthed")
        self.assertEqual((departed2["payload"]["eta_hour"], departed2["payload"]["etd_hour"]), (2, 5))
        self.assertEqual(departed2["state"], "departed")

    def test_extension_requires_reason_and_reschedules_again(self):
        v1 = self.create_voyage("VOY-1", 6, 12, vessel="Early")
        v2 = self.create_voyage("VOY-2", 12, 20, vessel="Late")
        m = self.create_maintenance("M-1", 10, 14)
        m = self.act(m, "confirm", {})
        v1 = self.service.get_record(CTL, v1["id"])
        v2 = self.service.get_record(CTL, v2["id"])
        self.assertEqual(v1["payload"]["eta_hour"], 14)
        self.assertEqual(v2["payload"]["eta_hour"], 20)  # 14-20 与 16-26 的v1冲突 → 20
        # 延期必须给原因
        with self.assertRaises(ValidationError):
            self.act(m, "extend", {"new_end_hour": 22})
        m = self.service.get_record(CREW, m["id"])
        m = self.act(m, "extend", {"new_end_hour": 22, "reason": "台风"})
        self.assertIn("台风", m["payload"]["extend_history"][-1]["reason"])
        v1 = self.service.get_record(CTL, v1["id"])
        v2 = self.service.get_record(CTL, v2["id"])
        self.assertEqual(v1["payload"]["eta_hour"], 22)
        self.assertEqual(v2["payload"]["eta_hour"], 28)  # FCFS 接在 v1 之后
        self.assertIn("台风", v1["payload"]["last_reschedule"]["reason"])

    def test_early_completion_releases_window_and_reverts(self):
        v = self.create_voyage("VOY-1", 6, 12)
        m = self.create_maintenance("M-1", 8, 20)
        m = self.act(m, "confirm", {})
        self.assertEqual(self.service.get_record(CTL, v["id"])["payload"]["eta_hour"], 20)
        m = self.service.get_record(CREW, m["id"])
        # 实际完工不能晚于计划结束
        with self.assertRaises(ValidationError):
            self.act(m, "complete", {"actual_end_hour": 21})
        m = self.service.get_record(CREW, m["id"])
        self.act(m, "complete", {"actual_end_hour": 15})
        v = self.service.get_record(CTL, v["id"])
        self.assertEqual(v["payload"]["eta_hour"], 15)  # 提前完工释放，回排
        self.assertEqual(v["payload"]["etd_hour"], 21)
        reasons = [e["reason"] for e in v["payload"]["reschedule_history"]]
        self.assertTrue(any("提前" in r and "完工" in r for r in reasons))

    def test_new_voyage_under_lock_is_queued_behind(self):
        v1 = self.create_voyage("VOY-1", 6, 10)
        m = self.create_maintenance("M-1", 8, 16)
        self.act(m, "confirm", {})
        # 后来的船申请 12-18（撞上封锁，且排在 v1 之后）
        v2 = self.create_voyage("VOY-2", 12, 18, vessel="Newcomer")
        v1 = self.service.get_record(CTL, v1["id"])
        v2 = self.service.get_record(CTL, v2["id"])
        self.assertEqual(v1["payload"]["eta_hour"], 16)
        self.assertEqual(v2["payload"]["eta_hour"], 20)  # 16 + v1占用到20
        self.assertEqual(v2["payload"]["original_eta_hour"], 12)

    def test_overlapping_active_maintenance_rejected(self):
        m1 = self.create_maintenance("M-1", 8, 16)
        self.act(m1, "confirm", {})
        m2 = self.create_maintenance("M-2", 14, 20)
        with self.assertRaises(Conflict):
            self.act(m2, "confirm", {})
        # 不同泊位的维修单可同时确认
        m3 = self.create_maintenance("M-3", berth="B99")
        confirmed = self.act(m3, "confirm", {})
        self.assertEqual(confirmed["state"], "confirmed")

    def test_draft_maintenance_does_not_lock(self):
        v = self.create_voyage("VOY-1", 6, 18)
        self.create_maintenance("M-1", 8, 16)  # 只登记不确认
        v = self.service.get_record(CTL, v["id"])
        self.assertEqual(v["payload"]["eta_hour"], 6)
        board = self.service.board(CTL)
        self.assertEqual(board["locks"], [])

    def test_board_shows_locks_waitlist_and_results(self):
        v1 = self.create_voyage("VOY-1", 6, 10, vessel="First")
        self.create_voyage("VOY-2", 10, 14, vessel="Second")
        m = self.create_maintenance("M-1", 8, 16)
        self.act(m, "confirm", {})
        board = self.service.board(CTL)
        self.assertEqual(len(board["locks"]), 1)
        self.assertEqual(board["locks"][0]["block_reason"], "岸桥轨道更换")
        self.assertEqual(board["locks"][0]["end_hour"], 16)
        wait = {w["reference"]: w for w in board["waitlist"]}
        self.assertEqual(wait["VOY-1"]["rank"], 1)
        self.assertEqual(wait["VOY-2"]["rank"], 2)
        self.assertEqual(wait["VOY-1"]["eta_hour"], 16)
        self.assertEqual(wait["VOY-2"]["eta_hour"], 20)
        triggers = {r["trigger"] for r in board["reschedule_results"]}
        self.assertEqual(triggers, {"maintenance_confirmed"})
        # 泊位过滤
        self.assertEqual(self.service.board(CTL, berth="B99")["waitlist"], [])

    def test_cancel_and_depart_rebuild_queue(self):
        early = self.create_voyage("VOY-1", 6, 12)
        later = self.create_voyage("VOY-2", 12, 20)
        m = self.create_maintenance("M-1", 4, 10)
        self.act(m, "confirm", {})
        early = self.service.get_record(CTL, early["id"])
        later = self.service.get_record(CTL, later["id"])
        self.assertEqual(early["payload"]["eta_hour"], 10)
        self.assertEqual(later["payload"]["eta_hour"], 16)
        # 头船取消，后者保持 16（原计划 12 撞上维修 4-10 否，12不撞；其desired 12, 占位的early移除后 → 12）
        self.service.act(CTL, early["id"], early["version"], "cancel", {"cancel_reason": "船方取消"})
        later = self.service.get_record(CTL, later["id"])
        self.assertEqual(later["payload"]["eta_hour"], 12)

    def test_stale_version_guard_still_applies(self):
        m = self.create_maintenance()
        with self.assertRaises(Conflict):
            self.service.act(CREW, m["id"], m["version"] - 1, "confirm", {})


class SchedulerRulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def test_queue_places_at_nearest_window_after_blocks(self):
        queue = [{"id": 1, "start": 6, "duration": 6}, {"id": 2, "start": 7, "duration": 4}]
        locks = [(8, 16, 99)]
        plan = self.rules.schedule_queue(queue, locks, [])
        self.assertEqual(plan[0]["eta_hour"], 16)
        self.assertEqual(plan[1]["eta_hour"], 22)  # FCFS，排在第一艘之后

    def test_hard_occupancy_blocks(self):
        queue = [{"id": 1, "start": 6, "duration": 12}]
        plan = self.rules.schedule_queue(queue, [], [(6, 18, 7)])
        self.assertEqual(plan[0]["eta_hour"], 18)


if __name__ == "__main__":
    unittest.main()

"""港口泊位与航道调度领域规则与状态转换。"""
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Conflict, ValidationError, boolean, choice, integer, number, text


INITIAL_STATE = "draft"
CREATE_ROLES = {'port_controller'}
ACTION_ROLES = {'confirm': {'port_controller'}, 'berth': {'port_controller'}, 'depart': {'port_controller'}, 'cancel': {'port_controller'}}
TRANSITIONS = {'confirm': {'draft': 'confirmed'}, 'berth': {'confirmed': 'berthed'}, 'depart': {'berthed': 'departed'}, 'cancel': {'draft': 'cancelled', 'confirmed': 'cancelled'}}

MAINTENANCE_CREATE_ROLES = {'maintenance_crew'}
MAINTENANCE_ACTION_ROLES = {'confirm': {'maintenance_crew'}, 'extend': {'maintenance_crew'}, 'complete': {'maintenance_crew'}, 'cancel': {'maintenance_crew'}}
MAINTENANCE_TRANSITIONS = {
    'confirm': {'draft': 'confirmed'},
    'extend': {'confirmed': 'extended', 'extended': 'extended'},
    'complete': {'confirmed': 'completed', 'extended': 'completed'},
    'cancel': {'draft': 'cancelled'},
}
ACTIVE_MAINTENANCE_STATES = {'confirmed', 'extended'}
WAITING_VOYAGE_STATES = {'draft', 'confirmed'}
MAX_HOUR = 72  # 重排允许跨天，超出24的小时数表示次日


class DomainRules:
    INITIAL_STATE = INITIAL_STATE
    ACTIVE_MAINTENANCE_STATES = ACTIVE_MAINTENANCE_STATES
    WAITING_VOYAGE_STATES = WAITING_VOYAGE_STATES

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES) | set(MAINTENANCE_CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        for roles in MAINTENANCE_ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str, kind: str = "voyage") -> bool:
        if role == "admin":
            return True
        return role in (MAINTENANCE_CREATE_ROLES if kind == "maintenance" else CREATE_ROLES)

    def role_can_action(self, role: str, action: str, kind: str = "voyage") -> bool:
        if role == "admin":
            return True
        table = MAINTENANCE_ACTION_ROLES if kind == "maintenance" else ACTION_ROLES
        return role in table.get(action, set())

    # ---- 航次靠泊计划 ----

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        vessel = text(p, "vessel")
        berth = text(p, "berth")
        vessel_length = number(p, "vessel_length_m", 1)
        berth_length = number(p, "berth_length_m", 1)
        draft = number(p, "draft_m", 0)
        berth_depth = number(p, "berth_depth_m", 0)
        eta = integer(p, "eta_hour", 0, 23)
        etd = integer(p, "etd_hour", 1, 24)
        choice(p, "risk_level", ["low", "medium", "high"])
        dangerous = boolean(p, "dangerous_goods")
        if etd <= eta:
            raise ValidationError("etd_hour必须晚于eta_hour")
        if berth_length < vessel_length:
            raise ValidationError("泊位长度不足")
        if berth_depth - draft < 0.5:
            raise ValidationError("剩余水深不足")
        if dangerous:
            text(p, "dangerous_class")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p["safety_margin_m"] = round(float(p["berth_depth_m"]) - float(p["draft_m"]), 2)
        p["window_hours"] = int(p["etd_hour"]) - int(p["eta_hour"])
        p["quay_ok"] = bool(p["berth_length_m"] >= p["vessel_length_m"] and p["safety_margin_m"] >= 0.5)
        # 保留船方原始计划窗口，调度重排只改 eta_hour/etd_hour，原始窗口作为候泊排序与回排依据
        p["original_eta_hour"] = int(p["eta_hour"])
        p["original_etd_hour"] = int(p["etd_hour"])
        p["reschedule_history"] = []
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item.get("kind", "voyage") != "voyage" or item["state"] in {"cancelled", "departed"}:
                continue
            other = item["payload"]
            if other.get("berth") != payload.get("berth"):
                continue
            if item["state"] == "berthed":
                other_start = int(other.get("eta_hour", 0))
                other_end = int(other.get("etd_hour", 24))
            else:
                # 候泊计划按船方原始窗口比较：被维修推后不应阻塞后来者申请更早的窗口
                other_start = int(other.get("original_eta_hour", other.get("eta_hour", 0)))
                other_end = int(other.get("original_etd_hour", other.get("etd_hour", 24)))
            if self.overlaps(int(payload["eta_hour"]), int(payload["etd_hour"]), other_start, other_end):
                raise Conflict("同一泊位时间窗冲突")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "confirm":
            pilot = text(data, "pilot_id")
            changes["pilot_id"] = pilot
            summary = "已确认引航员"
        elif action == "berth":
            actual = number(data, "actual_draft_m", 0)
            if float(p["berth_depth_m"]) - actual < 0.5:
                raise ValidationError("实际吃水导致水深不足")
            changes["actual_draft_m"] = actual
            summary = "船舶已靠泊"
        elif action == "depart":
            if not boolean(data, "cargo_operation_complete"):
                raise ValidationError("货物作业尚未完成")
            summary = "船舶已离泊"
        elif action == "cancel":
            changes["cancel_reason"] = text(data, "cancel_reason")
            summary = "计划已取消"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    # ---- 维修（泊位封锁）单 ----

    def prepare_maintenance(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "berth")
        start = integer(p, "start_hour", 0, 23)
        end = integer(p, "end_hour", 1, 24)
        if end <= start:
            raise ValidationError("end_hour必须晚于start_hour")
        text(p, "work")
        p["window_hours"] = end - start
        p["extend_history"] = []
        return p

    def check_maintenance_conflicts(self, berth: str, start: int, end: int, existing: Iterable[Dict[str, Any]], exclude_id: int = None) -> None:
        for item in existing:
            if item.get("kind") != "maintenance" or item["state"] not in ACTIVE_MAINTENANCE_STATES:
                continue
            if exclude_id is not None and int(item["id"]) == int(exclude_id):
                continue
            other = item["payload"]
            if other.get("berth") != berth:
                continue
            if self.overlaps(start, end, int(other["start_hour"]), int(other["end_hour"])):
                raise Conflict("该泊位在重叠时段已有封锁中的维护单")

    def require_maintenance_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = MAINTENANCE_TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("维修单当前状态不允许执行%s" % action)
        return allowed

    def apply_maintenance_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_maintenance_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        start = int(p["start_hour"])
        summary = ""
        if action == "confirm":
            summary = "维护单已确认，泊位%s将于%s-%s时封锁（%s）" % (p["berth"], start, p["end_hour"], p.get("work", ""))
        elif action == "extend":
            current_end = int(p["end_hour"])
            new_end = integer(data, "new_end_hour", current_end + 1, MAX_HOUR)
            reason = text(data, "reason")
            history = list(p.get("extend_history", []))
            history.append({"from_end_hour": current_end, "to_end_hour": new_end, "reason": reason, "at": datetime.now(timezone.utc).isoformat()})
            p["end_hour"] = new_end
            p["extend_reason"] = reason
            p["extend_history"] = history
            summary = "维护延期至%s时，原因：%s" % (new_end, reason)
        elif action == "complete":
            planned_end = int(p["end_hour"])
            actual = data.get("actual_end_hour", planned_end)
            if isinstance(actual, bool) or not isinstance(actual, int):
                raise ValidationError("actual_end_hour必须是整数")
            if not (start < actual <= planned_end):
                raise ValidationError("actual_end_hour必须晚于start_hour且不晚于计划完工时刻")
            p["actual_end_hour"] = actual
            p["completed_early"] = actual < planned_end
            summary = "维护提前于%s时完工，窗口释放" % actual if actual < planned_end else "维护已按期完工，窗口释放"
        elif action == "cancel":
            p["cancel_reason"] = text(data, "cancel_reason")
            summary = "维护单已取消"
        return new_state, p, summary

    # ---- 候泊调度 ----

    @staticmethod
    def overlaps(start_a: int, end_a: int, start_b: int, end_b: int) -> bool:
        return start_a < end_b and end_a > start_b

    @staticmethod
    def desired_window(payload: Dict[str, Any]) -> Tuple[int, int]:
        start = int(payload.get("original_eta_hour", payload["eta_hour"]))
        end = int(payload.get("original_etd_hour", payload.get("etd_hour", start + 1)))
        return start, end

    def schedule_queue(self, queue: List[Dict[str, Any]], locks: List[Tuple[int, int, Any]], hard_occupancies: List[Tuple[int, int, Any]]) -> List[Dict[str, int]]:
        """按候泊顺序把每条计划放到不早于期望时刻的最近可用窗口。

        queue: [{"id","start","duration"}]，已按候泊顺序排好；
        locks/hard_occupancies: (start, end, ref) 区间，维修封锁与在泊船舶不可占用。
        """
        blocked = list(locks) + list(hard_occupancies)
        placed: List[Tuple[int, int, Any]] = []
        result: List[Dict[str, int]] = []
        for item in queue:
            candidate = int(item["start"])
            duration = int(item["duration"])
            while True:
                hits = [iv for iv in blocked + placed if self.overlaps(candidate, candidate + duration, iv[0], iv[1])]
                if not hits:
                    break
                candidate = max(int(iv[1]) for iv in hits)
            placed.append((candidate, candidate + duration, item["id"]))
            result.append({"id": item["id"], "eta_hour": candidate, "etd_hour": candidate + duration})
        return result

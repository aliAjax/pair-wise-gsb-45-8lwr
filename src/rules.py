"""港口泊位与航道调度领域规则与状态转换。"""
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "draft"
CREATE_ROLES = {'port_controller'}
ACTION_ROLES = {'confirm': {'port_controller'}, 'berth': {'port_controller'}, 'depart': {'port_controller'}, 'cancel': {'port_controller'}}
TRANSITIONS = {'confirm': {'draft': 'confirmed'}, 'berth': {'confirmed': 'berthed'}, 'depart': {'berthed': 'departed'}, 'cancel': {'draft': 'cancelled', 'confirmed': 'cancelled'}}

MAINTENANCE_INITIAL_STATE = "draft"
MAINTENANCE_CREATE_ROLES = {'maintenance_planner', 'port_controller'}
MAINTENANCE_ACTION_ROLES = {'confirm': {'maintenance_planner', 'port_controller'}, 'extend': {'maintenance_planner', 'port_controller'}, 'complete': {'maintenance_planner', 'port_controller'}, 'cancel': {'maintenance_planner', 'port_controller'}}
MAINTENANCE_TRANSITIONS = {'confirm': {'draft': 'locked'}, 'extend': {'locked': 'locked'}, 'complete': {'locked': 'completed'}, 'cancel': {'draft': 'cancelled', 'locked': 'cancelled'}}
RESCHEDULABLE_STATES = {'draft', 'confirmed'}
ARCHIVED_STATES = {'cancelled', 'departed'}
SCHEDULE_HORIZON_HOUR = 48


def windows_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and a_end > b_start


class DomainRules:
    INITIAL_STATE = INITIAL_STATE
    MAINTENANCE_INITIAL_STATE = MAINTENANCE_INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        all_roles.update(MAINTENANCE_CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        for roles in MAINTENANCE_ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def role_can_create_maintenance(self, role: str) -> bool:
        return role == "admin" or role in MAINTENANCE_CREATE_ROLES

    def role_can_maintenance_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in MAINTENANCE_ACTION_ROLES.get(action, set())

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
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            other = item["payload"]
            if item["state"] in {"cancelled", "departed"} or other.get("berth") != payload.get("berth"):
                continue
            if int(payload["eta_hour"]) < int(other.get("etd_hour", 0)) and int(payload["etd_hour"]) > int(other.get("eta_hour", 24)):
                raise Conflict("同一泊位时间窗冲突")

    def check_lock_conflicts(self, payload: Dict[str, Any], locked_orders: Iterable[Dict[str, Any]]) -> None:
        for order in locked_orders:
            other = order["payload"]
            if other.get("berth") != payload.get("berth"):
                continue
            if windows_overlap(int(payload["eta_hour"]), int(payload["etd_hour"]), int(other.get("start_hour", 0)), int(other.get("end_hour", 24))):
                raise Conflict("泊位%s在%s-%s时处于维护封锁（%s）" % (payload.get("berth"), other.get("start_hour"), other.get("end_hour"), order["reference"]))

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

    def validate_maintenance(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "berth")
        integer(p, "start_hour", 0, 23)
        integer(p, "end_hour", 1, 24)
        text(p, "work_content")
        if int(p["end_hour"]) <= int(p["start_hour"]):
            raise ValidationError("end_hour必须晚于start_hour")
        return p

    def check_maintenance_overlap(self, payload: Dict[str, Any], locked_orders: Iterable[Dict[str, Any]]) -> None:
        for order in locked_orders:
            other = order["payload"]
            if other.get("berth") != payload.get("berth"):
                continue
            if windows_overlap(int(payload["start_hour"]), int(payload["end_hour"]), int(other.get("start_hour", 0)), int(other.get("end_hour", 24))):
                raise Conflict("泊位%s已被维护单%s封锁" % (payload.get("berth"), order["reference"]))

    def require_maintenance_transition(self, order: Dict[str, Any], action: str) -> str:
        allowed = MAINTENANCE_TRANSITIONS.get(action, {}).get(order["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_maintenance_action(self, order: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_maintenance_transition(order, action)
        data = dict(data or {})
        p = dict(order["payload"])
        summary = ""
        if action == "confirm":
            summary = "维护封锁已生效"
        elif action == "extend":
            new_end = integer(data, "new_end_hour", 1, SCHEDULE_HORIZON_HOUR)
            reason = text(data, "reason")
            if new_end <= int(p["end_hour"]):
                raise ValidationError("new_end_hour必须晚于当前结束时刻")
            extensions = list(p.get("extensions", []))
            extensions.append({"from_hour": int(p["end_hour"]), "to_hour": new_end, "reason": reason})
            p["extensions"] = extensions
            p["end_hour"] = new_end
            summary = "维护延期至%s时" % new_end
        elif action == "complete":
            note = optional_text(data, "note")
            if note:
                p["complete_note"] = note
            summary = "维护完工，封锁窗口已释放"
        elif action == "cancel":
            p["cancel_reason"] = text(data, "cancel_reason")
            summary = "维护单已取消"
        return new_state, p, summary or ("已执行%s" % action)

    def plan_berth_reschedule(self, records: List[Dict[str, Any]], locks: List[Dict[str, Any]], horizon: int = SCHEDULE_HORIZON_HOUR) -> List[Dict[str, Any]]:
        """计算单个泊位的重排方案。

        records：该泊位全部航次记录；locks：已生效封锁窗口（reference/start_hour/end_hour）。
        仅draft/confirmed航次可重排，berthed为固定障碍，cancelled/departed不参与。
        返回按候泊顺序排列的移动列表，每项含record、from_window、to_window、lock。
        """
        lock_windows = [(int(lock["start_hour"]), int(lock["end_hour"]), lock["reference"]) for lock in locks]
        fixed: List[Tuple[int, int]] = []
        movable: List[Tuple[Dict[str, Any], int, int]] = []
        for record in records:
            state = record["state"]
            if state in ARCHIVED_STATES:
                continue
            eta = int(record["payload"]["eta_hour"])
            etd = int(record["payload"]["etd_hour"])
            if state in RESCHEDULABLE_STATES:
                movable.append((record, eta, etd))
            else:
                fixed.append((eta, etd))
        placed = list(fixed)
        waiting: List[Tuple[Dict[str, Any], int, int]] = []
        for record, eta, etd in sorted(movable, key=lambda item: (item[1], item[2], item[0]["id"])):
            if any(windows_overlap(eta, etd, lock_start, lock_end) for lock_start, lock_end, _ref in lock_windows):
                waiting.append((record, eta, etd))
            else:
                placed.append((eta, etd))
        moves: List[Dict[str, Any]] = []
        for record, eta, etd in waiting:
            duration = etd - eta
            blocking = [(ls, le, ref) for ls, le, ref in lock_windows if windows_overlap(eta, etd, ls, le)]
            lock_ref = max(blocking, key=lambda item: item[1])[2]
            start = max(lock_end for _ls, lock_end, _ref in blocking)
            blockers = placed + [(ls, le) for ls, le, _ref in lock_windows]
            slot = start
            while slot + duration <= horizon and any(windows_overlap(slot, slot + duration, bs, be) for bs, be in blockers):
                slot += 1
            if slot + duration > horizon:
                raise Conflict("航次%s在%s小时内无可用窗口，无法重排" % (record["reference"], horizon))
            placed.append((slot, slot + duration))
            moves.append({"record": record, "from_window": (eta, etd), "to_window": (slot, slot + duration), "lock": lock_ref})
        return moves

"""业务用例编排、权限检查、维修封锁与候泊重排调度。"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, text
from .repository import Repository
from .rules import ACTIVE_MAINTENANCE_STATES, WAITING_VOYAGE_STATES, DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    # ---- 航次靠泊计划 ----

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role, "voyage"):
            raise PermissionDenied("角色无权创建靠泊计划")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        record = self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id, kind="voyage")
        # 新计划撞上维修封锁不拒绝：登记后由调度统一排到最近可用窗口
        updated = self._rebuild_schedule(
            actor, "voyage_created", {"reference": reference, "berth": prepared["berth"]}
        )
        return updated.get(record["id"], record)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit, kind=kind)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    # ---- 维修（泊位封锁）单 ----

    def create_maintenance(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role, "maintenance"):
            raise PermissionDenied("角色无权登记维护单")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_maintenance(payload or {})
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id, kind="maintenance")

    def list_maintenances(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit, kind="maintenance")

    # ---- 动作分发 ----

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        record = self.repository.get(record_id)
        kind = record.get("kind", "voyage")
        if not self.rules.role_can_action(actor.role, action, kind):
            raise PermissionDenied("角色无权执行该操作")
        if kind == "maintenance":
            return self._act_maintenance(actor, record, expected_version, action, data or {})
        return self._act_voyage(actor, record, expected_version, action, data or {})

    def _act_voyage(self, actor: Actor, record: Dict[str, Any], expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        new_state, new_payload, summary = self.rules.apply_action(record, action, data)
        acted = self.repository.mutate(
            record_id=record["id"],
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data, "from": record["state"], "to": new_state},
        )
        # 离泊释放泊位、取消退出队列，其余候泊计划可随之回排；确认/靠泊不改变泊位占用
        if action in {"depart", "cancel"}:
            self._rebuild_schedule(
                actor,
                "voyage_%s" % action,
                {"reference": record["reference"], "berth": record["payload"].get("berth", "")},
            )
        return acted

    def _act_maintenance(self, actor: Actor, record: Dict[str, Any], expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        new_state, new_payload, summary = self.rules.apply_maintenance_action(record, action, data)
        prospective_lock = None
        if action in {"confirm", "extend"}:
            self.rules.check_maintenance_conflicts(
                new_payload["berth"], int(new_payload["start_hour"]), int(new_payload["end_hour"]),
                self.repository.list_records(limit=500), exclude_id=record["id"],
            )
            prospective_lock = (int(new_payload["start_hour"]), int(new_payload["end_hour"]), record["id"])
        acted = self.repository.mutate(
            record_id=record["id"],
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data, "from": record["state"], "to": new_state},
        )
        # 取消仅针对草稿（从未封锁），无需重排；确认/延期/提前完工都要重算候泊队列
        if action != "cancel":
            self._rebuild_schedule(
                actor,
                "maintenance_%s" % new_state,
                {"maintenance_id": record["id"], "reference": record["reference"], "payload": dict(new_payload)},
                trigger_maintenance_id=record["id"],
                prospective_lock=prospective_lock,
            )
        return acted

    # ---- 候泊重排 ----

    def _active_locks(self, records: List[Dict[str, Any]], trigger_id: Optional[int], prospective_lock: Optional[Tuple[int, int, int]]) -> Dict[str, List[Tuple[int, int, int]]]:
        locks: Dict[str, List[Tuple[int, int, int]]] = {}
        for item in records:
            if item.get("kind") != "maintenance" or item["state"] not in ACTIVE_MAINTENANCE_STATES:
                continue
            if trigger_id is not None and int(item["id"]) == int(trigger_id):
                continue
            p = item["payload"]
            locks.setdefault(p["berth"], []).append((int(p["start_hour"]), int(p["end_hour"]), int(item["id"])))
        if prospective_lock is not None:
            start, end, maintenance_id = prospective_lock
            # 触发单已先落库（触发记录在读取时被排除），这里用可能变化后的窗口补回，保证封锁只计一次
            trigger = next(item for item in records if int(item["id"]) == int(maintenance_id))
            locks.setdefault(trigger["payload"]["berth"], []).append((start, end, maintenance_id))
        for intervals in locks.values():
            intervals.sort()
        return locks

    def _rebuild_schedule(
        self,
        actor: Actor,
        trigger: str,
        trigger_context: Dict[str, Any],
        trigger_maintenance_id: Optional[int] = None,
        prospective_lock: Optional[Tuple[int, int, int]] = None,
    ) -> Dict[int, Dict[str, Any]]:
        """把各泊位候泊航次按FCFS排到最近可用窗口，只持久化窗口发生变化的计划。

        已靠泊航次作为硬占用参与计算但本身不动；已离泊/已取消不参与（照旧留档）。
        """
        records = self.repository.list_records(limit=500)
        locks = self._active_locks(records, trigger_maintenance_id, prospective_lock)
        maintenance_index = {
            int(item["id"]): item for item in records if item.get("kind") == "maintenance"
        }

        waiting = [
            item for item in records
            if item.get("kind", "voyage") == "voyage" and item["state"] in WAITING_VOYAGE_STATES
        ]
        berthed = [item for item in records if item.get("kind", "voyage") == "voyage" and item["state"] == "berthed"]
        # 已完工维修实际占用到 actual_end_hour，作为历史硬占用，候泊船不能排进已被占掉的窗口
        finished = [
            item for item in records
            if item.get("kind") == "maintenance" and item["state"] == "completed" and item["payload"].get("actual_end_hour") is not None
        ]

        berths = {item["payload"]["berth"] for item in waiting}
        updated: Dict[int, Dict[str, Any]] = {}
        for berth in berths:
            berth_waiting = [item for item in waiting if item["payload"]["berth"] == berth]
            berth_waiting.sort(key=lambda item: (self.rules.desired_window(item["payload"])[0], item["id"]))
            queue = []
            for item in berth_waiting:
                start, end = self.rules.desired_window(item["payload"])
                queue.append({"id": item["id"], "start": start, "duration": end - start})
            hard = [
                (int(item["payload"]["eta_hour"]), int(item["payload"]["etd_hour"]), int(item["id"]))
                for item in berthed if item["payload"]["berth"] == berth
            ]
            hard.extend(
                (int(item["payload"]["start_hour"]), int(item["payload"]["actual_end_hour"]), int(item["id"]))
                for item in finished if item["payload"]["berth"] == berth
            )
            plan = {p["id"]: p for p in self.rules.schedule_queue(queue, locks.get(berth, []), hard)}
            for item in berth_waiting:
                target = plan[item["id"]]
                current_start = int(item["payload"]["eta_hour"])
                current_end = int(item["payload"]["etd_hour"])
                if target["eta_hour"] == current_start and target["etd_hour"] == current_end:
                    continue
                reason = self._reschedule_reason(
                    trigger, trigger_context, current_start, current_end,
                    target["eta_hour"], target["etd_hour"], berth,
                    locks.get(berth, []), maintenance_index,
                )
                updated[item["id"]] = self._persist_reschedule(actor, item, target, trigger, reason, trigger_context)
        return updated

    @staticmethod
    def _reschedule_reason(
        trigger: str,
        ctx: Dict[str, Any],
        old_start: int,
        old_end: int,
        new_start: int,
        new_end: int,
        berth: str,
        locks: List[Tuple[int, int, int]],
        maintenance_index: Dict[int, Dict[str, Any]],
    ) -> str:
        earlier = new_start < old_start
        if trigger == "maintenance_completed":
            p = ctx.get("payload", {})
            if p.get("completed_early"):
                return "维护单%s提前于%s时完工，剩余窗口释放，靠泊回排至%s时" % (
                    ctx.get("reference", ""), p.get("actual_end_hour"), new_start)
            return "维护单%s已完工，泊位恢复可用，靠泊排至%s时" % (ctx.get("reference", ""), new_start)
        if trigger == "voyage_depart" and earlier:
            return "在泊船舶离泊释放窗口，靠泊回排至%s时" % new_start

        blocking = next((iv for iv in locks if DomainRules.overlaps(old_start, old_end, iv[0], iv[1])), None)
        if blocking is None:
            blocking = next((iv for iv in locks if iv[1] > old_start), None)
        if blocking is not None:
            maint = maintenance_index.get(int(blocking[2]))
            payload = ctx.get("payload") if ctx.get("maintenance_id") == int(blocking[2]) else (maint["payload"] if maint else {})
            reference = payload.get("reference") or (maint["reference"] if maint else "")
            work = payload.get("work", "")
            if trigger == "maintenance_extended" and ctx.get("maintenance_id") == int(blocking[2]):
                label = "维护单%s延期至%s时（原因：%s）" % (reference, blocking[1], payload.get("extend_reason", ""))
            elif trigger == "maintenance_confirmed" and ctx.get("maintenance_id") == int(blocking[2]):
                label = "维护单%s确认封锁泊位（%s）" % (reference, work)
            else:
                label = "维护单%s封锁泊位（%s）" % (reference, work)
            return ("%s结束后窗口重算，靠泊调整至%s时" if earlier else "%s，靠泊顺延至%s时") % (label, new_start)

        if trigger == "voyage_cancel":
            return "计划取消后候泊队列重排，靠泊时刻调整为%s时" % new_start
        if trigger == "voyage_depart":
            return "在泊船舶离泊后候泊队列重排，靠泊时刻调整为%s时" % new_start
        if trigger == "voyage_created":
            return "候泊队列更新，靠泊时刻调整为%s时" % new_start
        return "泊位窗口重算，靠泊时刻调整为%s时" % new_start

    def _persist_reschedule(
        self, actor: Actor, record: Dict[str, Any], target: Dict[str, int], trigger: str, reason: str, ctx: Dict[str, Any]
    ) -> Dict[str, Any]:
        payload = dict(record["payload"])
        old_start, old_end = int(payload["eta_hour"]), int(payload["etd_hour"])
        at = datetime.now(timezone.utc).isoformat()
        entry = {
            "at": at,
            "trigger": trigger,
            "reason": reason,
            "from_eta_hour": old_start,
            "from_etd_hour": old_end,
            "to_eta_hour": target["eta_hour"],
            "to_etd_hour": target["etd_hour"],
        }
        if ctx.get("maintenance_id") is not None:
            entry["maintenance_id"] = ctx["maintenance_id"]
            entry["maintenance_reference"] = ctx.get("reference", "")
        history = list(payload.get("reschedule_history", []))
        history.append(entry)
        payload["eta_hour"] = target["eta_hour"]
        payload["etd_hour"] = target["etd_hour"]
        payload["window_hours"] = target["etd_hour"] - target["eta_hour"]
        payload["reschedule_history"] = history[-20:]
        payload["last_reschedule"] = entry
        return self.repository.mutate(
            record_id=record["id"],
            expected_version=int(record["version"]),
            state=record["state"],
            payload=payload,
            actor_id=actor.user_id,
            action="rescheduled",
            details={
                "summary": reason,
                "trigger": trigger,
                "from_eta_hour": old_start,
                "from_etd_hour": old_end,
                "to_eta_hour": target["eta_hour"],
                "to_etd_hour": target["etd_hour"],
            },
        )

    # ---- 看板与只读查询 ----

    def board(self, actor: Actor, berth: Optional[str] = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        records = self.repository.list_records(limit=500)
        maintenances = [item for item in records if item.get("kind") == "maintenance"]
        voyages = [item for item in records if item.get("kind", "voyage") == "voyage"]

        locks = []
        for item in maintenances:
            if item["state"] not in ACTIVE_MAINTENANCE_STATES:
                continue
            p = item["payload"]
            if berth and p["berth"] != berth:
                continue
            locks.append({
                "id": item["id"],
                "reference": item["reference"],
                "state": item["state"],
                "berth": p["berth"],
                "start_hour": int(p["start_hour"]),
                "end_hour": int(p["end_hour"]),
                "work": p.get("work", ""),
                "block_reason": p.get("work", ""),
                "extend_reason": p.get("extend_reason", ""),
                "updated_by": item["updated_by"],
                "updated_at": item["updated_at"],
            })
        locks.sort(key=lambda item: (item["berth"], item["start_hour"]))

        waiting = [item for item in voyages if item["state"] in WAITING_VOYAGE_STATES and (not berth or item["payload"]["berth"] == berth)]
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for item in waiting:
            grouped.setdefault(item["payload"]["berth"], []).append(item)
        waitlist = []
        for berth_name, items in grouped.items():
            items.sort(key=lambda it: (self.rules.desired_window(it["payload"])[0], it["id"]))
            for rank, item in enumerate(items, start=1):
                p = item["payload"]
                original_start, original_end = self.rules.desired_window(p)
                last = p.get("last_reschedule")
                waitlist.append({
                    "rank": rank,
                    "berth": berth_name,
                    "id": item["id"],
                    "reference": item["reference"],
                    "vessel": p.get("vessel", ""),
                    "state": item["state"],
                    "original_eta_hour": original_start,
                    "original_etd_hour": original_end,
                    "eta_hour": int(p["eta_hour"]),
                    "etd_hour": int(p["etd_hour"]),
                    "moved": int(p["eta_hour"]) != original_start,
                    "last_reason": last.get("reason", "") if last else "",
                    "last_reschedule_at": last.get("at", "") if last else "",
                })
        waitlist.sort(key=lambda item: (item["berth"], item["rank"]))

        events = self.repository.recent_events_by_action(["rescheduled"], limit=50)
        voyage_index = {item["id"]: item for item in voyages}
        reschedule_results = []
        for event in events:
            voyage = voyage_index.get(event["record_id"])
            if berth and (not voyage or voyage["payload"].get("berth") != berth):
                continue
            details = event["details"]
            reschedule_results.append({
                "record_id": event["record_id"],
                "reference": event["reference"],
                "vessel": voyage["payload"].get("vessel", "") if voyage else "",
                "berth": voyage["payload"].get("berth", "") if voyage else "",
                "at": event["created_at"],
                "by": event["actor_id"],
                "trigger": details.get("trigger", ""),
                "reason": details.get("summary", ""),
                "from_eta_hour": details.get("from_eta_hour"),
                "to_eta_hour": details.get("to_eta_hour"),
                "to_etd_hour": details.get("to_etd_hour"),
            })

        return {"locks": locks, "waitlist": waitlist, "reschedule_results": reschedule_results}

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        by_kind = self.repository.stats_by_kind()
        flat: Dict[str, int] = {}
        for states in by_kind.values():
            for state, total in states.items():
                flat[state] = flat.get(state, 0) + total
        flat["by_kind"] = by_kind
        return flat

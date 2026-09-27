"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, text
from .repository import Repository
from .rules import DomainRules


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

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_lock_conflicts(prepared, self.repository.list_maintenance(state="locked", berth=prepared["berth"], limit=500))
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        if action == "confirm":
            self.rules.check_lock_conflicts(record["payload"], self.repository.list_maintenance(state="locked", berth=record["payload"].get("berth", ""), limit=500))
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    def create_maintenance(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create_maintenance(actor.role):
            raise PermissionDenied("角色无权创建维护单")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.validate_maintenance(payload or {})
        self.rules.check_maintenance_overlap(prepared, self.repository.list_maintenance(state="locked", berth=prepared["berth"], limit=500))
        return self.repository.create_maintenance(reference, prepared["berth"], self.rules.MAINTENANCE_INITIAL_STATE, prepared, actor.user_id)

    def list_maintenance(self, actor: Actor, state: Optional[str] = None, berth: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_maintenance(state=state, berth=berth, limit=limit)

    def get_maintenance(self, actor: Actor, order_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_maintenance(order_id)

    def act_maintenance(self, actor: Actor, order_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_maintenance_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        data = dict(data or {})
        order = self.repository.get_maintenance(order_id)
        new_state, new_payload, summary = self.rules.apply_maintenance_action(order, action, data)
        plan_updates: List[Dict[str, Any]] = []
        result: Dict[str, Any] = {"moves": []}
        if action in ("confirm", "extend"):
            locks = [item for item in self.repository.list_maintenance(state="locked", berth=new_payload["berth"], limit=500) if item["id"] != order_id]
            self.rules.check_maintenance_overlap(new_payload, locks)
            lock_views = [{"reference": item["reference"], "start_hour": item["payload"]["start_hour"], "end_hour": item["payload"]["end_hour"]} for item in locks]
            lock_views.append({"reference": order["reference"], "start_hour": int(new_payload["start_hour"]), "end_hour": int(new_payload["end_hour"])})
            berth_records = [item for item in self.repository.list_records(limit=500) if item["payload"].get("berth") == new_payload["berth"]]
            reason = data.get("reason") if action == "extend" else None
            if reason:
                result["reason"] = reason
            for move in self.rules.plan_berth_reschedule(berth_records, lock_views):
                record = move["record"]
                plan_payload = dict(record["payload"])
                if "original_eta_hour" not in plan_payload:
                    plan_payload["original_eta_hour"] = int(plan_payload["eta_hour"])
                    plan_payload["original_etd_hour"] = int(plan_payload["etd_hour"])
                plan_payload["eta_hour"], plan_payload["etd_hour"] = move["to_window"]
                plan_payload["displaced_by"] = order["reference"]
                detail = {
                    "summary": "因维护单%s重排靠泊窗口" % order["reference"],
                    "maintenance": order["reference"],
                    "from": list(move["from_window"]),
                    "to": list(move["to_window"]),
                    "lock": move["lock"],
                }
                if reason:
                    plan_payload["displace_reason"] = reason
                    detail["reason"] = reason
                plan_updates.append({
                    "record_id": record["id"],
                    "expected_version": record["version"],
                    "state": record["state"],
                    "payload": plan_payload,
                    "action": "reschedule",
                    "details": detail,
                })
                result["moves"].append({
                    "record_id": record["id"],
                    "reference": record["reference"],
                    "vessel": plan_payload.get("vessel", ""),
                    "from": list(move["from_window"]),
                    "to": list(move["to_window"]),
                })
        elif action in ("complete", "cancel") and order["state"] == "locked":
            result["released"] = [int(order["payload"]["start_hour"]), int(order["payload"]["end_hour"])]
        new_payload["last_result"] = result
        details = {"summary": summary, "input": data, "from": order["state"], "to": new_state, "result": result}
        return self.repository.mutate_maintenance_with_plans(
            order_id=order_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details=details,
            plan_updates=plan_updates,
        )

    def maintenance_timeline(self, actor: Actor, order_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.maintenance_timeline(order_id)

    def berth_queue(self, actor: Actor, berth: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        berth = text({"berth": berth}, "berth")
        locks = self.repository.list_maintenance(state="locked", berth=berth, limit=500)
        records = [item for item in self.repository.list_records(limit=500) if item["payload"].get("berth") == berth and item["state"] not in ("cancelled", "departed")]
        queue = sorted(records, key=lambda item: (int(item["payload"]["eta_hour"]), int(item["payload"]["etd_hour"]), item["id"]))
        return {
            "berth": berth,
            "locks": [
                {
                    "reference": order["reference"],
                    "state": order["state"],
                    "start_hour": order["payload"]["start_hour"],
                    "end_hour": order["payload"]["end_hour"],
                    "work_content": order["payload"]["work_content"],
                }
                for order in sorted(locks, key=lambda item: (int(item["payload"]["start_hour"]), item["id"]))
            ],
            "queue": [
                {
                    "record_id": record["id"],
                    "reference": record["reference"],
                    "vessel": record["payload"].get("vessel", ""),
                    "state": record["state"],
                    "eta_hour": record["payload"]["eta_hour"],
                    "etd_hour": record["payload"]["etd_hour"],
                    "displaced_by": record["payload"].get("displaced_by", ""),
                    "original_eta_hour": record["payload"].get("original_eta_hour"),
                    "original_etd_hour": record["payload"].get("original_etd_hour"),
                }
                for record in queue
            ],
        }

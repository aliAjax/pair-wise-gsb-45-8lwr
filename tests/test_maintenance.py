import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


VOYAGE = {'vessel': 'HaiYun', 'berth': 'B12', 'vessel_length_m': 180, 'berth_length_m': 220, 'draft_m': 10.2, 'berth_depth_m': 11.5, 'eta_hour': 6, 'etd_hour': 10, 'risk_level': 'medium', 'dangerous_goods': False, 'dangerous_class': ''}
MAINTENANCE = {'berth': 'B12', 'start_hour': 8, 'end_hour': 14, 'work_content': '吊机检修'}
PLANNER = Actor('planner', 'maintenance_planner')
CONTROLLER = Actor('controller', 'port_controller')


def make_voyage(eta, etd, berth='B12'):
    data = dict(VOYAGE)
    data['eta_hour'] = eta
    data['etd_hour'] = etd
    data['berth'] = berth
    return data


def make_maintenance(start, end, berth='B12'):
    data = dict(MAINTENANCE)
    data['start_hour'] = start
    data['end_hour'] = end
    data['berth'] = berth
    return data


class MaintenanceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def create_and_lock(self, reference='M-1', start=8, end=14):
        order = self.service.create_maintenance(PLANNER, reference, make_maintenance(start, end))
        return self.service.act_maintenance(PLANNER, order['id'], order['version'], 'confirm', {})

    def test_confirm_locks_window_and_reschedules(self):
        first = self.service.create(CONTROLLER, 'VOY-1', make_voyage(6, 10))
        second = self.service.create(CONTROLLER, 'VOY-2', make_voyage(12, 16))
        order = self.create_and_lock()
        self.assertEqual(order['state'], 'locked')
        moves = order['payload']['last_result']['moves']
        self.assertEqual([move['reference'] for move in moves], ['VOY-1', 'VOY-2'])
        first = self.service.get_record(CONTROLLER, first['id'])
        second = self.service.get_record(CONTROLLER, second['id'])
        self.assertEqual((first['payload']['eta_hour'], first['payload']['etd_hour']), (14, 18))
        self.assertEqual((second['payload']['eta_hour'], second['payload']['etd_hour']), (18, 22))
        self.assertEqual(first['payload']['displaced_by'], 'M-1')
        self.assertEqual(first['payload']['original_eta_hour'], 6)
        self.assertEqual(first['payload']['original_etd_hour'], 10)
        timeline = self.service.timeline(CONTROLLER, first['id'])
        self.assertEqual(timeline[-1]['action'], 'reschedule')
        self.assertEqual(timeline[-1]['details']['maintenance'], 'M-1')
        self.assertEqual(timeline[-1]['details']['from'], [6, 10])
        self.assertEqual(timeline[-1]['details']['to'], [14, 18])

    def test_berthed_and_departed_stay_archived(self):
        berthed = self.service.create(CONTROLLER, 'VOY-B', make_voyage(6, 12))
        berthed = self.service.act(CONTROLLER, berthed['id'], berthed['version'], 'confirm', {'pilot_id': 'P-1'})
        berthed = self.service.act(CONTROLLER, berthed['id'], berthed['version'], 'berth', {'actual_draft_m': 10.3})
        departed = self.service.create(CONTROLLER, 'VOY-D', make_voyage(12, 14))
        departed = self.service.act(CONTROLLER, departed['id'], departed['version'], 'confirm', {'pilot_id': 'P-2'})
        departed = self.service.act(CONTROLLER, departed['id'], departed['version'], 'berth', {'actual_draft_m': 10.3})
        departed = self.service.act(CONTROLLER, departed['id'], departed['version'], 'depart', {'cargo_operation_complete': True})
        waiting = self.service.create(CONTROLLER, 'VOY-W', make_voyage(13, 17))
        self.create_and_lock()
        berthed = self.service.get_record(CONTROLLER, berthed['id'])
        departed = self.service.get_record(CONTROLLER, departed['id'])
        waiting = self.service.get_record(CONTROLLER, waiting['id'])
        self.assertEqual((berthed['payload']['eta_hour'], berthed['payload']['etd_hour']), (6, 12))
        self.assertNotIn('displaced_by', berthed['payload'])
        self.assertEqual((departed['payload']['eta_hour'], departed['payload']['etd_hour']), (12, 14))
        self.assertNotIn('displaced_by', departed['payload'])
        self.assertEqual((waiting['payload']['eta_hour'], waiting['payload']['etd_hour']), (14, 18))

    def test_lock_blocks_new_plans_until_completed(self):
        order = self.create_and_lock()
        with self.assertRaises(Conflict):
            self.service.create(CONTROLLER, 'VOY-X', make_voyage(10, 12))
        order = self.service.act_maintenance(PLANNER, order['id'], order['version'], 'complete', {'note': '提前完工'})
        self.assertEqual(order['state'], 'completed')
        self.assertEqual(order['payload']['last_result']['released'], [8, 14])
        created = self.service.create(CONTROLLER, 'VOY-X', make_voyage(10, 12))
        self.assertEqual(created['state'], 'draft')

    def test_extend_requeues_waiting_ships_with_reason(self):
        first = self.service.create(CONTROLLER, 'VOY-1', make_voyage(6, 10))
        second = self.service.create(CONTROLLER, 'VOY-2', make_voyage(10, 14))
        order = self.create_and_lock(start=8, end=12)
        first = self.service.get_record(CONTROLLER, first['id'])
        second = self.service.get_record(CONTROLLER, second['id'])
        self.assertEqual((first['payload']['eta_hour'], first['payload']['etd_hour']), (12, 16))
        self.assertEqual((second['payload']['eta_hour'], second['payload']['etd_hour']), (16, 20))
        order = self.service.act_maintenance(PLANNER, order['id'], order['version'], 'extend', {'new_end_hour': 16, 'reason': '配件延误'})
        self.assertEqual(order['payload']['end_hour'], 16)
        self.assertEqual(order['payload']['extensions'][0]['reason'], '配件延误')
        self.assertEqual(order['payload']['extensions'][0]['from_hour'], 12)
        first = self.service.get_record(CONTROLLER, first['id'])
        second = self.service.get_record(CONTROLLER, second['id'])
        self.assertEqual((first['payload']['eta_hour'], first['payload']['etd_hour']), (20, 24))
        self.assertEqual(first['payload']['displace_reason'], '配件延误')
        self.assertEqual((second['payload']['eta_hour'], second['payload']['etd_hour']), (16, 20))
        timeline = self.service.timeline(CONTROLLER, first['id'])
        self.assertEqual(timeline[-1]['action'], 'reschedule')
        self.assertEqual(timeline[-1]['details']['reason'], '配件延误')
        order_events = self.service.maintenance_timeline(PLANNER, order['id'])
        self.assertEqual(order_events[-1]['action'], 'extend')
        self.assertEqual(order_events[-1]['details']['input']['reason'], '配件延误')

    def test_cancel_releases_lock(self):
        order = self.create_and_lock()
        order = self.service.act_maintenance(PLANNER, order['id'], order['version'], 'cancel', {'cancel_reason': '船厂计划取消'})
        self.assertEqual(order['state'], 'cancelled')
        created = self.service.create(CONTROLLER, 'VOY-X', make_voyage(10, 12))
        self.assertEqual(created['state'], 'draft')

    def test_overlapping_locks_rejected(self):
        self.create_and_lock()
        with self.assertRaises(Conflict):
            self.service.create_maintenance(PLANNER, 'M-2', make_maintenance(10, 12))
        second = self.service.create_maintenance(PLANNER, 'M-2', make_maintenance(14, 18))
        second = self.service.act_maintenance(PLANNER, second['id'], second['version'], 'confirm', {})
        self.assertEqual(second['state'], 'locked')

    def test_extend_cannot_overlap_other_lock(self):
        first = self.create_and_lock(reference='M-1', start=8, end=12)
        self.create_and_lock(reference='M-2', start=14, end=18)
        with self.assertRaises(Conflict):
            self.service.act_maintenance(PLANNER, first['id'], first['version'], 'extend', {'new_end_hour': 16, 'reason': '赶工'})

    def test_extend_beyond_horizon_fails_and_order_unchanged(self):
        self.service.create(CONTROLLER, 'VOY-1', make_voyage(6, 10))
        order = self.create_and_lock(start=8, end=12)
        with self.assertRaises(Conflict):
            self.service.act_maintenance(PLANNER, order['id'], order['version'], 'extend', {'new_end_hour': 48, 'reason': '严重延误'})
        order = self.service.get_maintenance(PLANNER, order['id'])
        self.assertEqual(order['state'], 'locked')
        self.assertEqual(order['payload']['end_hour'], 12)

    def test_permission_and_validation(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_maintenance(Actor('outsider', 'outsider'), 'M-1', make_maintenance(8, 14))
        with self.assertRaises(ValidationError):
            self.service.create_maintenance(PLANNER, 'M-1', make_maintenance(14, 8))
        order = self.create_and_lock()
        with self.assertRaises(ValidationError):
            self.service.act_maintenance(PLANNER, order['id'], order['version'], 'extend', {'new_end_hour': 10, 'reason': 'x'})
        with self.assertRaises(ValidationError):
            self.service.act_maintenance(PLANNER, order['id'], order['version'], 'extend', {'new_end_hour': 20})
        with self.assertRaises(Conflict):
            self.service.act_maintenance(PLANNER, order['id'], order['version'] + 5, 'complete', {})
        with self.assertRaises(Conflict):
            self.service.create_maintenance(PLANNER, 'M-1', make_maintenance(16, 20))

    def test_berth_queue_shows_locks_and_waiting_order(self):
        self.service.create(CONTROLLER, 'VOY-1', make_voyage(6, 10))
        self.service.create(CONTROLLER, 'VOY-2', make_voyage(12, 16))
        self.create_and_lock()
        view = self.service.berth_queue(CONTROLLER, 'B12')
        self.assertEqual(view['locks'][0]['reference'], 'M-1')
        self.assertEqual(view['locks'][0]['work_content'], '吊机检修')
        self.assertEqual([item['reference'] for item in view['queue']], ['VOY-1', 'VOY-2'])
        self.assertEqual(view['queue'][0]['eta_hour'], 14)
        self.assertEqual(view['queue'][0]['displaced_by'], 'M-1')
        self.assertEqual(view['queue'][0]['original_eta_hour'], 6)
        self.assertEqual(view['queue'][1]['eta_hour'], 18)

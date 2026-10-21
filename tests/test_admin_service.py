"""Offline administrator preview, identity, rule and recovery invariants."""
from datetime import datetime, timedelta
import json

import pytest

from app.config import Settings
from app.db import Database
from app.security import SecretBox
from app.uestc import admin, checkin
from app.uestc.admin_service import AdminService
from app.uestc.errors import NetworkError, ServiceError
from app.uestc.http import UestcHttp


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = Settings(data_dir=tmp_path, db_path=tmp_path / 'test.db',
                   app_secret='test-secret', admin_password='test-password', scheduler_enabled=False)
    db = Database(cfg.db_path)
    box = SecretBox(cfg.app_secret)
    account_id = db.create_user(name='管理账号', student_id='admin', enabled=0,
                                password_enc=box.encrypt('password'))
    account = db.get_user(account_id)
    db.save_session(account_id, box.encrypt(json.dumps({'SESSION': 'offline'})))
    clock = [datetime(2026, 10, 21, 22, 0, tzinfo=cfg.tz)]
    state = {'record_id': 'dated-1', 'name': '学生甲', 'reports': [], 'classes': [{'classKey': 'c1'}], 'class_calls': 0}
    monkeypatch.setattr(checkin, 'warm_up', lambda client: None)
    monkeypatch.setattr(checkin, 'enter_app', lambda client: checkin.AppEntry(True, 200, 'offline'))
    monkeypatch.setattr(checkin, 'fetch_today', lambda client: pytest.fail('admin must not fetch personal today'))
    def classes(client):
        state['class_calls'] += 1
        return state['classes']
    monkeypatch.setattr(admin, 'fetch_my_classes', classes)
    def roster(client, business_date, class_key, **kwargs):
        rows = [{'id': state['record_id'], 'studentSn': '001', 'studentName': state['name']},
                {'id': 'other', 'studentSn': '0019', 'studentName': '学生乙'}]
        page = kwargs['page_index']
        return {'data': [rows[page - 1]], 'page': {'totalPage': 2}}
    monkeypatch.setattr(admin, 'fetch_checkin_list', roster)
    monkeypatch.setattr(admin, 'fetch_situation_types', lambda c: [{'label': '校内', 'value': 'CAMPUS'}])
    monkeypatch.setattr(admin, 'fetch_case_keywords', lambda c, code: [{'keyword': '学习', 'id': 'k1', 'remarkRequired': True}])
    def report(client, reports):
        state['reports'].append(reports)
        return True
    monkeypatch.setattr(admin, 'report_situations', report)
    service = AdminService(cfg, db, box, client_factory=lambda a: UestcHttp('offline'), now=lambda: clock[0])
    return service, db, account, clock, state


def preview(service, account, targets=None):
    return service.prepare(account, '2026-10-21', targets or [{'student_id': '001'}], '校内', '学习', '说明')


def test_preview_resolves_exact_target_and_submission_uses_fresh_record(env):
    service, db, account, clock, state = env
    prepared = preview(service, account)
    assert prepared['entries'][0]['student_id'] == '001'
    assert state['reports'] == []
    state['record_id'] = 'dated-2'
    result = service.submit(account, prepared['id'])
    assert result['status'] == 'acknowledged'
    assert state['reports'][0][0]['recordId'] == 'dated-2'
    assert db.list_logs(account['id'])[0]['event'] == 'admin_report'
    with pytest.raises(ServiceError, match='重复'):
        service.submit(account, prepared['id'])
    assert len(state['reports']) == 1


def test_preview_is_account_bound_and_expires(env):
    service, db, account, clock, state = env
    prepared = preview(service, account)
    other_id = db.create_user(student_id='other', enabled=0)
    with pytest.raises(ValueError, match='当前账号'):
        db.claim_report_preview(prepared['id'], other_id, int(clock[0].timestamp()))
    clock[0] += timedelta(seconds=service.PREVIEW_TTL)
    with pytest.raises(ServiceError, match='过期'):
        service.submit(account, prepared['id'])
    assert state['reports'] == []


def test_batch_preflight_and_identity_changes_never_write(env):
    service, db, account, clock, state = env
    with pytest.raises(ServiceError, match='未找到'):
        preview(service, account, [{'student_id': '001'}, {'student_id': 'missing'}])
    prepared = preview(service, account)
    state['name'] = '不同人'
    with pytest.raises(ServiceError, match='未找到'):
        service.submit(account, prepared['id'])
    assert state['reports'] == []


def test_timeout_is_uncertain_and_once_only(env, monkeypatch):
    service, db, account, clock, state = env
    prepared = preview(service, account)
    calls = []
    def timeout(client, reports):
        calls.append(reports)
        raise NetworkError('timeout after send')
    monkeypatch.setattr(admin, 'report_situations', timeout)
    assert service.submit(account, prepared['id'])['status'] == 'uncertain'
    with pytest.raises(ServiceError, match='重复'):
        service.submit(account, prepared['id'])
    assert len(calls) == 1
    assert 'uncertain' in db.list_logs(account['id'])[0]['detail']


def test_metadata_refresh_removes_stale_permissions(env):
    service, db, account, clock, state = env
    assert service.classes(account) == [{'classKey': 'c1'}]
    state['classes'] = []
    assert service.classes(account) == [{'classKey': 'c1'}]
    assert service.classes(account, refresh=True) == []
    with pytest.raises(ServiceError, match='不属于'):
        service.roster(account, '2026-10-21', 'c1')


def test_rules_execute_without_personal_enabled_and_do_not_catch_up(env):
    service, db, account, clock, state = env
    rule_id = service.add_rule(account, '001', '校内', '学习', '说明',
                               '2026-10-20', '2026-10-25', '1,2,3,4,5,6,7', '21:00')
    service.tick(clock[0])
    service.tick(clock[0])
    assert [(j['rule_id'], j['business_date'], j['status']) for j in db.list_report_jobs()] == [
        (rule_id, '2026-10-21', 'acknowledged')]
    assert len(state['reports']) == 1


def test_rules_reject_cross_account_overlap_and_recovery_never_retries(env):
    service, db, account, clock, state = env
    other_id = db.create_user(student_id='other', enabled=0)
    args = ('001', '校内', '学习', '说明', '2026-10-21', '2026-10-21', '3', '21:00')
    first = db.create_report_rule(account['id'], *args)
    with pytest.raises(ValueError, match='重叠'):
        db.create_report_rule(other_id, *args)
    # A textual date overlap with no actual shared weekday is valid.
    db.create_report_rule(other_id, '001', '校内', '学习', '说明', '2026-10-21', '2026-10-21', '4', '21:00')
    job = db.claim_report_job(first, account['id'], '2026-10-21')
    db.finish_report_job(job, 'writing', 'sent')
    assert db.recover_report_jobs() == 1
    assert db.list_report_jobs()[0]['status'] == 'uncertain'
    service.tick(clock[0])
    assert state['reports'] == []


def test_rule_update_overlap_failure_preserves_original(env):
    service, db, account, clock, state = env
    first = service.add_rule(account, '001', '校内', '学习', '说明', '2026-10-21', '2026-10-21', '3', '21:00')
    second = db.create_report_rule(account['id'], '001', '校内', '学习', '说明', '2026-10-22', '2026-10-22', '4', '21:00')
    with pytest.raises(ServiceError, match='重叠'):
        service.update_rule(account, second, '001', '校内', '学习', '说明', '2026-10-21', '2026-10-21', '3', '22:00')
    assert db.list_report_rules()[1]['start_date'] == '2026-10-22'


def test_enabling_conflicting_rule_is_rejected_and_claim_survives_reopen(env):
    service, db, account, clock, state = env
    args = ('001', '校内', '学习', '说明', '2026-10-21', '2026-10-21', '3', '21:00')
    first = db.create_report_rule(account['id'], *args)
    second = db.create_report_rule(account['id'], *args, enabled=False)
    with pytest.raises(ValueError, match='重叠'):
        db.set_report_rule_enabled(second, True)
    assert db.list_report_rules()[1]['enabled'] == 0
    job = db.claim_report_job(first, account['id'], '2026-10-21')
    reopened = Database(db.path)
    assert reopened.claim_report_job(first, account['id'], '2026-10-21') is None
    assert reopened.list_report_jobs()[0]['id'] == job


def test_simultaneous_job_claim_has_one_owner(env):
    from concurrent.futures import ThreadPoolExecutor
    service, db, account, clock, state = env
    rule = db.create_report_rule(account['id'], '001', '校内', '学习', '说明',
                                 '2026-10-21', '2026-10-21', '3', '21:00')
    with ThreadPoolExecutor(max_workers=2) as workers:
        claims = list(workers.map(lambda _: db.claim_report_job(rule, account['id'], '2026-10-21'), range(2)))
    assert sum(value is not None for value in claims) == 1


def test_personal_today_invalid_session_reauthenticates_once(env, monkeypatch):
    from app.uestc.errors import SessionInvalid
    service, db, account, clock, state = env
    personal = service._session
    reads = []
    logins = []
    expected = object()
    def fetch(client):
        reads.append(True)
        if len(reads) == 1:
            raise SessionInvalid('expired at today endpoint')
        return expected
    monkeypatch.setattr(checkin, 'fetch_today', fetch)
    monkeypatch.setattr(personal, '_perform_login', lambda *args: logins.append(True))
    with UestcHttp('offline') as client:
        today, fresh = personal._prepare(account, client, clock[0])
    assert today is expected
    assert fresh is True
    assert logins == [True]
    assert len(reads) == 2

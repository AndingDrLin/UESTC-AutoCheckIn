"""Admin console behavior with local storage and no university network calls."""
from __future__ import annotations

import json
import re
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.uestc.errors import ServiceError
from app.uestc.service import Outcome


@pytest.fixture()
def app(tmp_path):
    return create_app(Settings(data_dir=tmp_path / 'data', db_path=tmp_path / 'data' / 'test.db',
                               admin_password='s3cret', app_secret='test-secret', scheduler_enabled=False))


def csrf_from(html):
    match = re.search(r'name="_csrf" value="([^"]+)"', html)
    assert match
    return match.group(1)


def login(client):
    response = client.post('/login', data={'password': 's3cret'}, follow_redirects=False)
    assert response.status_code == 303


def add_account(client, app, student_id='2020001', **extra):
    csrf = csrf_from(client.get('/accounts/new').text)
    response = client.post('/accounts/new', data={'_csrf': csrf, 'name': '张三', 'student_id': student_id,
                                                 'password': 'cas-password', **extra}, follow_redirects=False)
    assert response.status_code == 303, response.text
    assert response.headers['location'].startswith('/accounts')
    return next(u for u in app.state.db.list_users() if u['student_id'] == student_id)


def test_authentication_and_offline_account_pages(app, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('plain local page must not contact university')
    monkeypatch.setattr(app.state.admin_service, 'classes', forbidden)
    monkeypatch.setattr(app.state.service, 'test', forbidden)
    with TestClient(app) as client:
        assert client.get('/', follow_redirects=False).headers['location'] == '/login'
        assert client.post('/login', data={'password': 'bad'}).status_code == 401
        login(client)
        assert client.get('/').status_code == 200
        account = add_account(client, app)
        assert account['enabled'] == 0
        assert account['latitude'] == account['longitude'] == 0
        assert client.get('/accounts').status_code == 200
        assert client.get(f"/accounts/{account['id']}/edit").status_code == 200
        assert client.get('/self').status_code == 200
        assert client.get('/rules').status_code == 200
        assert client.get('/logs').status_code == 200
        assert client.get('/settings').status_code == 200
        assert client.get('/healthz').json()['status'] == 'ok'


def test_personal_enabling_requires_coordinates(app):
    with TestClient(app) as client:
        login(client)
        a = add_account(client, app)
        csrf = csrf_from(client.get('/accounts').text)
        response = client.post(f"/accounts/{a['id']}/toggle", data={'_csrf': csrf}, follow_redirects=False)
        assert 'level=error' in response.headers['location']
        assert app.state.db.get_user(a['id'])['enabled'] == 0
        response = client.post('/accounts/new', data={'_csrf': csrf, 'name': '李四', 'student_id': '2020002',
                                                     'password': 'pw', 'enabled': '1'})
        assert response.status_code == 400
        assert app.state.db.count_users() == 1


def test_workspace_selection_and_explicit_preview(app, monkeypatch):
    calls = []
    monkeypatch.setattr(app.state.admin_service, 'classes', lambda a, refresh=False: calls.append(a['id']) or [{'classKey': 'C', 'className': '一班'}])
    monkeypatch.setattr(app.state.admin_service, 'roster', lambda a, *args: [{'classKey': 'C', 'studentSn': '12345', 'studentName': '学生甲', 'status': '未签到'}])
    monkeypatch.setattr(app.state.admin_service, 'options', lambda a, *args: {'types': [{'label': '校内', 'value': 'CAMPUS'}], 'keywords': [{'keyword': '学习', 'id': 'study', 'remarkRequired': True}]})
    with TestClient(app) as client:
        login(client)
        a = add_account(client, app)
        b = add_account(client, app, '2020002')
        assert calls == []
        page = client.get(f"/?account_id={b['id']}&business_date=2026-10-21")
        assert calls == [b['id']]
        assert '学生甲' in page.text
        csrf = csrf_from(page.text)
        def prepare(account, date, targets, location, reason, remark):
            assert account['id'] == b['id'] and date == '2026-10-21'
            assert targets == [{'student_id': '12345', 'class_key': 'C'}]
            return {'id': 'opaque', 'business_date': date, 'entries': [{'student_id': '12345', 'student_name': '学生甲', 'class_key': 'C', 'location': location, 'reason': reason, 'remark': remark}]}
        monkeypatch.setattr(app.state.admin_service, 'prepare', prepare)
        data = {'_csrf': csrf, 'account_id': b['id'], 'business_date': '2026-10-21', 'location': '校内', 'reason': '学习', 'remark': '图书馆'}
        empty = client.post('/reports/preview', data=data, follow_redirects=False)
        assert empty.status_code == 303 and 'level=error' in empty.headers['location']
        response = client.post('/reports/preview', data={**data, 'targets': json.dumps({'student_id': '12345', 'class_key': 'C'})})
        assert response.status_code == 200
        assert '2026-10-21' in response.text and 'opaque' in response.text
        monkeypatch.setattr(app.state.admin_service, 'submit', lambda account, token: {'status': 'uncertain', 'message': '连接中断，请人工核对', 'entries': []})
        response = client.post('/reports/submit', data={'_csrf': csrf, 'account_id': b['id'], 'preview_id': 'opaque'})
        assert response.status_code == 200 and '连接中断，请人工核对' in response.text
        assert client.get('/?account_id=99999').status_code == 404
        assert calls == [b['id']]


@pytest.mark.parametrize('path', ['/reports/preview', '/reports/submit', '/rules', '/rules/1/edit', '/rules/1/toggle', '/accounts/new', '/accounts/1/toggle', '/accounts/1/session/clear', '/logout'])
def test_csrf_rejects_mutations(app, path):
    with TestClient(app) as client:
        login(client)
        response = client.post(path, data={'_csrf': 'wrong', 'account_id': '1', 'preview_id': 'token'}, follow_redirects=False)
        assert response.status_code == 400


def test_rules_account_boundary_and_edit(app, monkeypatch):
    with TestClient(app) as client:
        login(client)
        a = add_account(client, app)
        b = add_account(client, app, '2020002')
        rule_id = app.state.db.create_report_rule(a['id'], '12345', '校内', '学习', '', '2026-10-21', '2026-10-21', '3', '22:00')
        csrf = csrf_from(client.get('/accounts').text)
        response = client.post(f'/rules/{rule_id}/delete', data={'_csrf': csrf, 'account_id': b['id']})
        assert response.status_code == 404
        assert len(app.state.db.list_report_rules(a['id'])) == 1
        assert '12345' not in client.get(f"/rules?account_id={b['id']}").text
        assert '12345' in client.get(f"/rules?account_id={a['id']}").text
        def reject(*args):
            raise ServiceError('重叠规则')
        monkeypatch.setattr(app.state.admin_service, 'update_rule', reject)
        response = client.post(f'/rules/{rule_id}/edit', data={'_csrf': csrf, 'account_id': a['id']}, follow_redirects=False)
        assert 'level=error' in response.headers['location']
        assert len(app.state.db.list_report_rules(a['id'])) == 1


def test_manual_personal_submission_does_not_require_automation(app, monkeypatch):
    class OpenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 21, 22, 0, tzinfo=tz)
    monkeypatch.setattr('app.main.datetime', OpenDatetime)
    observed = []
    monkeypatch.setattr(app.state.service, 'run', lambda account, manual: observed.append((account['id'], manual)) or Outcome(ok=True, message='已确认个人签到'))
    with TestClient(app) as client:
        login(client)
        a = add_account(client, app, latitude='30.75', longitude='103.93')
        assert a['enabled'] == 0
        csrf = csrf_from(client.get('/self').text)
        response = client.post(f"/accounts/{a['id']}/checkin", data={'_csrf': csrf}, follow_redirects=False)
        assert response.status_code == 303
        assert observed == [(a['id'], True)]
        client.post('/logout', data={'_csrf': csrf})
        assert client.get('/', follow_redirects=False).status_code == 303

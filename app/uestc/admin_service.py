"""Account-scoped class reporting with explicit previews and durable write claims."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime
import logging
import secrets
import threading

from . import admin
from .errors import ApiError, ServiceError
from .service import CheckinService

logger = logging.getLogger(__name__)


class AdminService:
    PREVIEW_TTL = 600
    METADATA_TTL = 300

    def __init__(self, settings, db, box, notifier=None, *, client_factory=None, now=None):
        self.settings, self.db, self.box = settings, db, box
        self.notifier = notifier
        self._now = now or (lambda: datetime.now(settings.tz))
        self._session = CheckinService(settings, db, box, notifier,
                                       client_factory=client_factory, now=self._now)
        self._cache = {}
        self._cache_lock = threading.RLock()
        self._tick_lock = threading.Lock()

    @staticmethod
    def _date(value):
        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError()
            return parsed
        except (ValueError, TypeError) as exc:
            raise ServiceError('日期必须为 YYYY-MM-DD') from exc

    @contextmanager
    def _client(self, account):
        try:
            with self._session.authenticated_client(account) as client:
                yield client
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError(str(exc)) from exc

    def _cached(self, account, kind, refresh, ttl, fetch):
        key = (int(account['id']), *kind)
        now = self._now().timestamp()
        with self._cache_lock:
            if refresh:
                self._cache.pop(key, None)
            cached = self._cache.get(key)
            if not refresh and cached and cached[0] > now:
                return deepcopy(cached[1])
        value = fetch()
        with self._cache_lock:
            self._cache[key] = (now + ttl, deepcopy(value))
        return value

    def _classes(self, account, client, refresh=False):
        return self._cached(account, ('classes',), refresh, self.METADATA_TTL,
                            lambda: admin.fetch_my_classes(client))

    def classes(self, account: dict, refresh: bool = False) -> list[dict]:
        with self._client(account) as client:
            return self._classes(account, client, refresh)

    def _roster(self, account, client, business_date, class_key='', refresh=False):
        classes = self._classes(account, client, refresh)
        keys = list(dict.fromkeys(str(item['classKey']) for item in classes))
        if class_key:
            if class_key not in keys:
                raise ServiceError('所选班级不属于当前账号的可管理班级')
            keys = [class_key]

        def fetch():
            rows = []
            for key in keys:
                page = 1
                while True:
                    envelope = admin.fetch_checkin_list(client, business_date, key,
                                                        page_index=page, page_size=100)
                    rows.extend(dict(row, classKey=key) for row in envelope['data'])
                    if page >= int(envelope['page']['totalPage']):
                        break
                    page += 1
            return rows
        # Dated record identifiers are deliberately never cached.
        return fetch()

    def roster(self, account: dict, business_date: str, class_key: str = '',
               keyword: str = '', refresh: bool = False) -> list[dict]:
        self._date(business_date)
        with self._client(account) as client:
            rows = self._roster(account, client, business_date, class_key, refresh)
        if keyword:
            rows = [row for row in rows if str(row.get('studentSn', '')) == keyword
                    or keyword in str(row.get('studentName', ''))]
        return rows

    @staticmethod
    def _unique(rows, description):
        if not rows:
            raise ServiceError(f'未找到{description}')
        if len(rows) != 1:
            raise ServiceError(f'{description}匹配不唯一，请提供学号或班级键')
        return rows[0]

    def _options(self, account, client, location='', refresh=False):
        types = self._cached(account, ('types',), refresh, self.METADATA_TTL,
                             lambda: admin.fetch_situation_types(client))
        keywords = []
        if location:
            kind = self._unique([item for item in types
                                 if location in (item['label'], item['value'])], '情况类型')
            code = kind['value']
            keywords = self._cached(account, ('keywords', code), refresh, self.METADATA_TTL,
                                    lambda: admin.fetch_case_keywords(client, code))
        return {'types': types, 'keywords': keywords}

    def options(self, account: dict, location: str = '', refresh: bool = False) -> dict:
        with self._client(account) as client:
            return self._options(account, client, location, refresh)

    def _resolve(self, account, client, business_date, targets, location, reason, remark, refresh):
        if not targets:
            raise ServiceError('请至少选择一名学生')
        options = self._options(account, client, location, refresh)
        kind = self._unique([item for item in options['types']
                             if location in (item['label'], item['value'])], '情况类型')
        keyword = self._unique([item for item in options['keywords']
                                if reason in (item['keyword'], str(item['id']))], '情况关键词')
        if keyword.get('remarkRequired') and not remark.strip():
            raise ServiceError('该情况关键词要求填写说明')
        rows = self._roster(account, client, business_date, refresh=refresh)
        result, seen = [], set()
        for target in targets:
            sid = str(target.get('student_id') or '').strip()
            name = str(target.get('student_name') or '').strip()
            key = str(target.get('class_key') or '')
            if not sid and not name:
                raise ServiceError('请提供学生学号或姓名')
            row = self._unique([row for row in rows
                                if (not sid or str(row.get('studentSn', '')) == sid)
                                and (not name or row.get('studentName') == name)
                                and (not key or row['classKey'] == key)], '学生记录')
            identity = (row['classKey'], str(row['studentSn']))
            if identity in seen:
                raise ServiceError('同一批次不可重复选择学生')
            seen.add(identity)
            result.append({'student_id': str(row['studentSn']), 'student_name': row['studentName'],
                           'class_key': row['classKey'], 'location': kind['label'],
                           'reason': keyword['keyword'], 'remark': remark,
                           'payload': {'classKey': row['classKey'], 'recordId': row['id'],
                                       'locationTypeCode': kind['value'], 'caseKeywordId': keyword['id'],
                                       'caseRemark': remark}})
        return result

    def prepare(self, account: dict, business_date: str, targets: list[dict], location: str,
                reason: str, remark: str) -> dict:
        self._date(business_date)
        with self._client(account) as client:
            entries = self._resolve(account, client, business_date, targets, location, reason, remark, True)
            preview = {'id': secrets.token_urlsafe(32), 'account_id': int(account['id']),
                       'business_date': business_date, 'entries': entries}
            self.db.save_report_preview(preview, int(self._now().timestamp()) + self.PREVIEW_TTL)
            return preview

    def _audit(self, account, preview, status, message, entries):
        self.db.finish_report_preview(preview['id'], status, message)
        self.db.add_log(message, user_id=int(account['id']), user_name=account.get('name', ''),
                        level='INFO' if status == 'acknowledged' else 'WARNING',
                        event='admin_report', detail={'preview_id': preview['id'], 'status': status,
                        'account_id': int(account['id']), 'business_date': preview['business_date'],
                        'entries': entries})
        return {'status': status, 'message': message, 'entries': entries}

    def submit(self, account: dict, preview_id: str) -> dict:
        with self._client(account) as client:
            try:
                preview = self.db.claim_report_preview(preview_id, int(account['id']), int(self._now().timestamp()))
            except ValueError as exc:
                raise ServiceError(str(exc)) from exc
            original = preview['entries']
            try:
                first = original[0]
                entries = self._resolve(account, client, preview['business_date'], original,
                                        first['location'], first['reason'], first['remark'], True)
            except Exception as exc:
                self._audit(account, preview, 'failed', f'提交前重新解析失败：{exc}', original)
                raise ServiceError(str(exc)) from exc
            self.db.finish_report_preview(preview_id, 'writing', '已开始提交，禁止重复使用')
            try:
                acknowledged = admin.report_situations(client, [entry['payload'] for entry in entries])
                if acknowledged is True:
                    status, message = 'acknowledged', '学校接口已确认接收；不代表已独立核验打卡成功'
                elif acknowledged is False:
                    status, message = 'failed', '学校接口未确认接收（data=false）'
                else:
                    status, message = 'uncertain', '提交响应缺少明确确认，请人工核对，禁止盲目重试'
            except ApiError as exc:
                status = 'uncertain' if str(exc.code) == '-1' else 'failed'
                message = f'提交结果不确定，请人工核对：{exc}' if status == 'uncertain' else f'学校接口拒绝提交：{exc}'
            except Exception as exc:
                status, message = 'uncertain', f'提交结果不确定，请人工核对，禁止盲目重试：{exc}'
            result = self._audit(account, preview, status, message, entries)
            try:
                self._roster(account, client, preview['business_date'], refresh=True)
            except Exception:
                logger.warning('post-report roster refresh failed', exc_info=True)
            return result

    def add_rule(self, account: dict, student_id, location, reason, remark, start_date,
                 end_date, weekdays, run_time, enabled=True) -> int:
        try:
            self.db.validate_report_rule(student_id, location, reason, start_date, end_date, weekdays, run_time)
            # Check current permissions and metadata, not a future record that may not exist yet.
            with self._client(account) as client:
                self._resolve(account, client, self._now().date().isoformat(),
                              [{'student_id': student_id}], location, reason, remark, True)
            rule_id = self.db.create_report_rule(int(account['id']), student_id, location, reason, remark,
                                                 start_date, end_date, weekdays, run_time, enabled)
            self.db.add_log('创建情况上报规则', user_id=int(account['id']), user_name=account.get('name', ''),
                            event='admin_rule', detail={'rule_id': rule_id, 'student_id': student_id, 'action': 'create'})
            return rule_id
        except (ValueError, TypeError) as exc:
            raise ServiceError(str(exc)) from exc

    def update_rule(self, account: dict, rule_id, student_id, location, reason, remark,
                    start_date, end_date, weekdays, run_time, enabled=True) -> None:
        try:
            self.db.validate_report_rule(student_id, location, reason, start_date, end_date, weekdays, run_time)
            with self._client(account) as client:
                self._resolve(account, client, self._now().date().isoformat(),
                              [{'student_id': student_id}], location, reason, remark, True)
            self.db.update_report_rule(rule_id, int(account['id']), student_id, location, reason, remark,
                                       start_date, end_date, weekdays, run_time, enabled)
            self.db.add_log('更新情况上报规则', user_id=int(account['id']), user_name=account.get('name', ''),
                            event='admin_rule', detail={'rule_id': rule_id, 'student_id': student_id, 'action': 'update'})
        except (ValueError, TypeError) as exc:
            raise ServiceError(str(exc)) from exc

    def tick(self, now=None):
        now = now or self._now()
        today = now.date().isoformat()
        with self._tick_lock:
            for rule in self.db.list_report_rules():
                if not rule['enabled'] or not rule['start_date'] <= today <= rule['end_date']:
                    continue
                if str(now.isoweekday()) not in rule['weekdays'].split(',') or now.strftime('%H:%M') < rule['run_time']:
                    continue
                job_id = self.db.claim_report_job(rule['id'], rule['account_id'], today)
                if job_id is None:
                    continue
                preview_id = None
                try:
                    account = self.db.get_user(rule['account_id'])
                    if account is None:
                        raise ServiceError('执行账号已删除')
                    preview = self.prepare(account, today, [{'student_id': rule['student_id']}],
                                           rule['location'], rule['reason'], rule['remark'])
                    preview_id = preview['id']
                    self.db.finish_report_job(job_id, 'writing', '开始提交，禁止自动重试', preview_id)
                    result = self.submit(account, preview_id)
                    self.db.finish_report_job(job_id, result['status'], result['message'], preview_id)
                except ServiceError as exc:
                    self.db.finish_report_job(job_id, 'failed', str(exc), preview_id)
                except Exception as exc:
                    self.db.finish_report_job(job_id, 'uncertain', f'执行中断，请人工核对：{exc}', preview_id)
                    logger.exception('report job failed')

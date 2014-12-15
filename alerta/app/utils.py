import json
import datetime
import pytz
import re

from datetime import timedelta
from flask import make_response, request, current_app
from functools import update_wrapper

from alerta.app import app
from alerta.alert import Alert

LOG = app.logger


PARAMS_EXCLUDE = [
    '_',
    'callback',
    'token',
    'api-key'
]


def parse_fields(r):

    query_time = datetime.datetime.utcnow()

    params = r.args.copy()

    for s in PARAMS_EXCLUDE:
        if s in params:
            del params[s]

    if params.get('q', None):
        query = json.loads(params['q'])
        del params['q']
    else:
        query = dict()

    if params.get('from-date', None):
        try:
            from_date = datetime.datetime.strptime(params['from-date'], '%Y-%m-%dT%H:%M:%S.%fZ')
        except ValueError, e:
            LOG.warning('Could not parse from_date query parameter: %s', e)
            raise
        from_date = from_date.replace(tzinfo=pytz.utc)
        to_date = query_time
        to_date = to_date.replace(tzinfo=pytz.utc)
        query['lastReceiveTime'] = {'$gt': from_date, '$lte': to_date}
        del params['from-date']

    if params.get('id', None):
        query['$or'] = [{'_id': {'$regex': '^' + params['id']}},
                        {'lastReceiveId': {'$regex': '^' + params['id']}}]
        del params['id']

    if params.get('duplicateCount', None):
        query['duplicateCount'] = int(params.get('duplicateCount'))
        del params['duplicateCount']

    if params.get('repeat', None):
        query['repeat'] = True if params.get('repeat', 'true') == 'true' else False
        del params['repeat']

    sort = list()
    direction = 1
    if params.get('reverse', None):
        direction = -1
        del params['reverse']
    if params.get('sort-by', None):
        for sort_by in params.getlist('sort-by'):
            if sort_by in ['createTime', 'receiveTime', 'lastReceiveTime']:
                sort.append((sort_by, -direction))  # reverse chronological
            else:
                sort.append((sort_by, direction))
        del params['sort-by']
    else:
        sort.append(('lastReceiveTime', -direction))

    group = list()
    if 'group-by' in params:
        group = params.get('group-by')
        del params['group-by']

    if 'limit' in params:
        limit = params.get('limit')
        del params['limit']
    else:
        limit = app.config['QUERY_LIMIT']
    limit = int(limit)

    for field in params:
        value = params.getlist(field)
        if len(value) == 1:
            value = value[0]
            if field.endswith('!'):
                if value.startswith('~'):
                    query[field[:-1]] = dict()
                    query[field[:-1]]['$not'] = re.compile(value[1:], re.IGNORECASE)
                else:
                    query[field[:-1]] = dict()
                    query[field[:-1]]['$ne'] = value
            else:
                if value.startswith('~'):
                    query[field] = dict()
                    query[field]['$regex'] = re.compile(value[1:], re.IGNORECASE)
                else:
                    query[field] = value
        else:
            if field.endswith('!'):
                if '~' in [v[0] for v in value]:
                    value = '|'.join([v.lstrip('~') for v in value])
                    query[field[:-1]] = dict()
                    query[field[:-1]]['$not'] = re.compile(value, re.IGNORECASE)
                else:
                    query[field[:-1]] = dict()
                    query[field[:-1]]['$nin'] = value
            else:
                if '~' in [v[0] for v in value]:
                    value = '|'.join([v.lstrip('~') for v in value])
                    query[field] = dict()
                    query[field]['$regex'] = re.compile(value, re.IGNORECASE)
                else:
                    query[field] = dict()
                    query[field]['$in'] = value

    return query, sort, group, limit, query_time


def crossdomain(origin=None, methods=None, headers=None,
                max_age=21600, attach_to_all=True,
                automatic_options=True):
    if methods is not None:
        methods = ', '.join(sorted(x.upper() for x in methods))
    if headers is not None and not isinstance(headers, basestring):
        headers = ', '.join(x.upper() for x in headers)
    if not isinstance(origin, basestring):
        origin = ', '.join(origin)
    if isinstance(max_age, timedelta):
        max_age = max_age.total_seconds()

    def get_methods():
        if methods is not None:
            return methods

        options_resp = current_app.make_default_options_response()
        return options_resp.headers['allow']

    def decorator(f):
        def wrapped_function(*args, **kwargs):
            if automatic_options and request.method == 'OPTIONS':
                resp = current_app.make_default_options_response()
            else:
                resp = make_response(f(*args, **kwargs))
            if not attach_to_all and request.method != 'OPTIONS':
                return resp

            h = resp.headers

            h['Access-Control-Allow-Origin'] = origin
            h['Access-Control-Allow-Methods'] = get_methods()
            h['Access-Control-Max-Age'] = str(max_age)
            if headers is not None:
                h['Access-Control-Allow-Headers'] = headers
            return resp

        f.provide_automatic_options = False
        return update_wrapper(wrapped_function, f)
    return decorator


def parse_notification(notification):

    notification = json.loads(notification)
    alarm = json.loads(notification['Message'])

    if 'Trigger' not in alarm:
        return

    # Defaults
    resource = '%s:%s' % (alarm['Trigger']['Dimensions'][0]['name'], alarm['Trigger']['Dimensions'][0]['value'])
    event = alarm['AlarmName']
    severity = cw_state_to_severity(alarm['NewStateValue'])
    group = 'CloudWatch'
    value = alarm['Trigger']['MetricName']
    text = alarm['AlarmDescription']
    service = [alarm['AWSAccountId']]
    tags = [alarm['Trigger']['Namespace']]
    correlate = list()
    origin = notification['TopicArn']
    timeout = None
    create_time = datetime.datetime.strptime(notification['Timestamp'], '%Y-%m-%dT%H:%M:%S.%fZ')
    raw_data = notification['Message']

    return Alert(
        resource=resource,
        event=event,
        environment='Production',
        severity=severity,
        correlate=correlate,
        service=service,
        group=group,
        value=value,
        text=text,
        tags=tags,
        attributes={
            'awsMessageId': notification['MessageId'],
            'awsRegion': alarm['Region'],
            'thresholdInfo': alarm['NewStateReason']
        },
        origin=origin,
        event_type='cloudwatchAlarm',
        create_time=create_time,
        timeout=timeout,
        raw_data=raw_data,
    )


def cw_state_to_severity(state):

    if state == 'ALARM':
        return 'major'
    elif state == 'INSUFFICIENT_DATA':
        return 'warning'
    elif state == 'OK':
        return 'normal'
    else:
        return 'unknown'

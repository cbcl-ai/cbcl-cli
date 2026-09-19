"""Disposable, network-isolated acceptance of the installed Claude CLI refresh.

All credentials and provider replies in this file are synthetic test fixtures.
The TLS provider is reachable only on this container's loopback interface.
"""

import json
import os
import argparse
import ast
from pathlib import Path
import re
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor


parser = argparse.ArgumentParser()
parser.add_argument('--concurrency', choices=[1, 2], type=int, default=1)
arguments = parser.parse_args()


ROOT = Path('/tmp/cli-refresh-fixture')
ROOT.mkdir(mode=0o755)
CERTIFICATE = ROOT / 'certificate.pem'
PRIVATE_KEY = ROOT / 'key.pem'
subprocess.run([
    'openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
    '-keyout', str(PRIVATE_KEY), '-out', str(CERTIFICATE), '-days', '1',
    '-subj', '/CN=api.anthropic.com', '-addext',
    'subjectAltName=DNS:api.anthropic.com,DNS:platform.claude.com,'
    'DNS:console.anthropic.com,DNS:claude.ai',
], check=True, capture_output=True)
CERTIFICATE.chmod(0o644)

OLD_ACCESS = 'sk-ant-oat01-fixture-expired-access'
OLD_REFRESH = 'sk-ant-ort01-fixture-old-refresh'
NEW_ACCESS = 'sk-ant-oat01-fixture-rotated-access'
NEW_REFRESH = 'sk-ant-ort01-fixture-rotated-refresh'
SCOPES = ['user:inference', 'user:profile', 'user:sessions:claude_code', 'user:mcp_servers']
PROFILE = {
    'account': {
        'uuid': '11111111-1111-4111-8111-111111111111',
        'email': 'synthetic@example.invalid', 'display_name': 'Synthetic Test',
        'has_claude_max': True, 'has_claude_pro': False,
    },
    'organization': {
        'uuid': '22222222-2222-4222-8222-222222222222',
        'organization_type': 'claude_max', 'rate_limit_tier': 'default_claude_max_20x',
    },
}
events = []
refresh_requests = []
refresh_lock = threading.Lock()


class Provider(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def reply(self, status, payload, headers=None):
        self.event['response_status'] = status
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def respond(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        auth = self.headers.get('Authorization', '')
        self.event = {'method': self.command, 'host': self.headers.get('Host'),
                      'path': self.path, 'rotated_access': auth == f'Bearer {NEW_ACCESS}'}
        events.append(self.event)
        if self.path.endswith('/oauth/token'):
            try:
                request = json.loads(body)
            except ValueError:
                from urllib.parse import parse_qs
                request = {key: values[0] for key, values in parse_qs(body.decode()).items()}
            with refresh_lock:
                refresh_requests.append(request)
                refresh_request_number = len(refresh_requests)
            if (refresh_request_number != 1 or request.get('grant_type') != 'refresh_token'
                    or request.get('refresh_token') != OLD_REFRESH):
                return self.reply(400, {'error': 'invalid_grant'})
            return self.reply(200, {
                'access_token': NEW_ACCESS, 'refresh_token': NEW_REFRESH,
                'expires_in': 28800, 'token_type': 'Bearer', 'scope': ' '.join(SCOPES),
            })
        if self.path.startswith('/api/oauth/profile'):
            return self.reply(200 if auth == f'Bearer {NEW_ACCESS}' else 401,
                              PROFILE if auth == f'Bearer {NEW_ACCESS}' else {'error': 'expired_token'})
        if self.path.startswith('/v1/messages'):
            user_id = json.loads(body).get('metadata', {}).get('user_id', '')
            session = re.search(r'(?:"session_id"\s*:\s*"|_session_)([0-9a-f-]+)', user_id)
            self.event['session_id'] = session.group(1) if session else None
            return self.reply(429, {'type': 'error', 'error': {
                'type': 'rate_limit_error',
                'message': "You've hit your limit. Try again after your usage window resets.",
            }}, {
                'retry-after': '1',
                'anthropic-ratelimit-unified-status': 'rejected',
                'anthropic-ratelimit-unified-reset': str(int(time.time() + 3600)),
            })
        return self.reply(200, {})

    do_GET = respond
    do_POST = respond


server = ThreadingHTTPServer(('127.0.0.1', 443), Provider)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(CERTIFICATE, PRIVATE_KEY)
server.socket = context.wrap_socket(server.socket, server_side=True)
threading.Thread(target=server.serve_forever, daemon=True).start()

auth_dir = Path('/home/agent/.claude')
auth_dir.mkdir(mode=0o700, exist_ok=True)
credentials = auth_dir / '.credentials.json'
assert not credentials.exists(), 'Fixture requires a pristine container'
old_expiry = int((time.time() - 3600) * 1000)
credentials.write_text(json.dumps({'claudeAiOauth': {
    'accessToken': OLD_ACCESS, 'refreshToken': OLD_REFRESH, 'expiresAt': old_expiry,
    'scopes': SCOPES, 'subscriptionType': 'max', 'rateLimitTier': 'default_claude_max_20x',
}}))
credentials.chmod(0o600)
os.chown(auth_dir, 1000, 1000)
os.chown(credentials, 1000, 1000)
cli_config = Path('/home/agent/.claude.json')
cli_config.write_text(json.dumps({
    'hasCompletedOnboarding': True, 'lastOnboardingVersion': '2.1.259',
    'oauthAccount': {'accountUuid': PROFILE['account']['uuid'],
                     'emailAddress': PROFILE['account']['email'],
                     'organizationUuid': PROFILE['organization']['uuid'],
                     'organizationRole': 'admin'},
}))
os.chown(cli_config, 1000, 1000)

environment = {
    'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/home/agent', 'LANG': 'C.UTF-8',
    'NODE_EXTRA_CA_CERTS': str(CERTIFICATE),
    'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1', 'DISABLE_AUTOUPDATER': '1',
}
version = subprocess.run(['claude', '--version'], env=environment, user=1000, group=1000,
                         capture_output=True, text=True, check=True).stdout.strip()
assert version == '2.1.259 (Claude Code)', version
auth_helpers = ast.parse((Path(__file__).parent / 'auth_helpers.py').read_text())
profile_assignment = next(
    item for item in auth_helpers.body if isinstance(item, ast.Assign)
    and any(isinstance(target, ast.Name) and target.id == '_AUTH_PROFILE_SCRIPT'
            for target in item.targets)
)
profile_script = ast.literal_eval(profile_assignment.value)


def profile_state():
    result = subprocess.run(['node', '-'], input=profile_script,
                            env=environment, user=1000, group=1000,
                            capture_output=True, text=True, timeout=10, check=True)
    return json.loads(result.stdout)['state']


profile_before = profile_state()
assert profile_before == 'invalid'
request = {
    'policy_version': 'cubicle-read-only-v1', 'profile': 'diagnostic',
    'system_prompt': 'Reply only with ok. Do not perform any action.',
    'user_prompt': 'ok', 'model': 'haiku', 'max_turns': 1, 'output_format': 'text',
    'timeout': 30,
}
# Read the policy version from the installed immutable image's actual launcher.
import runpy
runner_path = '/usr/local/libexec/cubicle/generation_runner.py'
request['policy_version'] = runpy.run_path(runner_path)['POLICY_VERSION']
started = time.monotonic()


def diagnostic(_):
    return subprocess.run([
        '/usr/local/bin/python3', '-I', '-S', runner_path,
    ], input=json.dumps(request), env=environment, user=1000, group=1000,
       capture_output=True, text=True, timeout=45)


with ThreadPoolExecutor(max_workers=arguments.concurrency) as executor:
    results = list(executor.map(diagnostic, range(arguments.concurrency)))
profile_after = profile_state()
saved = json.loads(credentials.read_text())['claudeAiOauth']
receipt = {
    'cli_version': version, 'duration_seconds': round(time.monotonic() - started, 2),
    'concurrency': arguments.concurrency,
    'diagnostics': [{'returncode': result.returncode, 'stdout': result.stdout[-1200:],
                     'stderr': result.stderr[-1200:]} for result in results],
    'profile_before': profile_before, 'profile_after': profile_after, 'requests': events,
    'refresh_request_count': len(refresh_requests),
    'access_token_rotated': saved.get('accessToken') == NEW_ACCESS,
    'refresh_token_rotated': saved.get('refreshToken') == NEW_REFRESH,
    'saved_expiry_advanced': saved.get('expiresAt', 0) > old_expiry,
    'saved_expiry_in_future': saved.get('expiresAt', 0) > int(time.time() * 1000),
    'model_request_used_rotated_access': any(
        event['path'].startswith('/v1/messages') and event['rotated_access'] for event in events
    ),
    'model_session_count': len({event['session_id'] for event in events
                               if event.get('session_id')}),
}
print(json.dumps(receipt, indent=2))
assert receipt['refresh_request_count'] == 1, 'Expected exactly one provider refresh'
assert receipt['access_token_rotated'] and receipt['refresh_token_rotated']
assert receipt['saved_expiry_advanced'] and receipt['saved_expiry_in_future']
assert receipt['model_request_used_rotated_access']
assert receipt['model_session_count'] == arguments.concurrency
assert all(event['rotated_access'] and event['response_status'] == 429
           for event in events if event['path'].startswith('/v1/messages'))
assert profile_after == 'valid'
assert all(result.returncode != 0 for result in results), 'All model requests are rejected'

"""Run against a deployed stack; logs in, uploads an image, and queues real inference."""
import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
environment = dict(line.split('=', 1) for line in (ROOT / '.env').read_text().splitlines() if '=' in line)
base = os.environ.get('STUDIO_URL', 'http://127.0.0.1:18088')
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def request(path, data=None, content_type='application/json'):
    req = urllib.request.Request(base + path, data=data,
                                 headers={'Content-Type': content_type} if data else {})
    with opener.open(req, timeout=30) as response:
        raw = response.read()
        return json.loads(raw) if 'application/json' in response.headers.get('Content-Type', '') else raw


assert request('/api/health')['status'] == 'ok'
request('/api/login', json.dumps({'username': environment['STUDIO_USER'], 'password': environment['STUDIO_PASSWORD']}).encode())
assert request('/api/system')['worker']['online']
boundary = 'MovaStudioEndToEndBoundary'
data = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="sample.png"\r\nContent-Type: image/png\r\n\r\n').encode()
data += (ROOT / 'data/uploads/sample.png').read_bytes() + f'\r\n--{boundary}--\r\n'.encode()
upload = request('/api/uploads', data, 'multipart/form-data; boundary=' + boundary)
job = request('/api/jobs', json.dumps({'prompt':'A man in a quiet office smiles gently at the camera. He says, "Hello, welcome to the studio." Soft indoor lighting and natural movement.', 'image_id':upload['image_id'], 'model':os.environ.get('E2E_MODEL', 'MOVA-720p'),'num_frames':int(os.environ.get('E2E_FRAMES', '49')),'steps':int(os.environ.get('E2E_STEPS', '4')),'seed':67}).encode())
(ROOT / 'inspection/e2e-job.json').write_text(json.dumps(job, ensure_ascii=False, indent=2))
print(json.dumps({'event':'submitted','id':job['id']}, ensure_ascii=False), flush=True)
previous = None
while True:
    job = request('/api/jobs/' + job['id'])
    state = (job['status'], job['stage'], job['progress'])
    if state != previous:
        print(json.dumps({'event':'progress', 'status':state[0], 'stage':state[1], 'progress':state[2]}, ensure_ascii=False), flush=True)
        previous = state
    if job['status'] in ('succeeded', 'failed', 'cancelled'):
        break
    time.sleep(10)
if job['status'] != 'succeeded':
    raise RuntimeError(job.get('error') or job['status'])
video = request(job['video_url'])
assert len(video) > 1024 and b'ftyp' in video[:64]
(ROOT / 'inspection/e2e-result.json').write_text(json.dumps({'job':job, 'download_bytes':len(video), 'elapsed_seconds':job['finished'] - job['started']}, ensure_ascii=False, indent=2))
print(json.dumps({'event':'passed', 'id':job['id'], 'download_bytes':len(video), 'elapsed_seconds':round(job['finished'] - job['started'], 2)}), flush=True)

"""API and queue regression checks, independent of the GPU model."""
import importlib
import io
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('STUDIO_PASSWORD', 'test-password-for-api-only')
    monkeypatch.setenv('SESSION_SECRET', 'a' * 64)
    import store
    import studio
    importlib.reload(store)
    importlib.reload(studio)
    with TestClient(studio.app) as api:
        Image.new('RGB', (64, 64), 'green').save(tmp_path / 'uploads' / 'sample.png')
        store.heartbeat({'heartbeat': time.time(), 'models': ['MOVA-720p'], 'gpus': '6,7'})
        yield api, store


def login(api):
    assert api.post('/api/login', json={'username': 'lj', 'password': 'test-password-for-api-only'}).status_code == 200


def test_auth_and_media_protection(client):
    api, store = client
    assert api.get('/api/jobs').status_code == 401
    assert api.get('/api/uploads/sample.png').status_code == 401
    assert api.get('/api/health').json()['status'] == 'ok'
    assert api.post('/api/login', json={'username': 'lj', 'password': 'wrong'}).status_code == 401
    login(api)
    assert api.get('/api/session').status_code == 200
    assert api.get('/api/uploads/sample.png').status_code == 200
    api.post('/api/logout')
    assert api.get('/api/session').status_code == 401


def test_upload_validation_and_normalization(client):
    api, store = client
    login(api)
    assert api.post('/api/uploads', files={'file': ('bad.png', b'not an image', 'image/png')}).status_code == 422
    image = io.BytesIO()
    Image.new('RGB', (80, 40), 'blue').save(image, 'JPEG')
    response = api.post('/api/uploads', files={'file': ('../../escape.jpg', image.getvalue(), 'image/jpeg')})
    assert response.status_code == 200
    assert response.json()['image_id'].endswith('.png')
    assert api.get(response.json()['url']).headers['content-type'] == 'image/png'
    assert not (store.DATA.parent / 'escape.jpg').exists()


def test_queue_claim_cancel_and_capacity(client):
    api, store = client
    login(api)
    payload = {'prompt': 'A person smiles at the camera.', 'steps': 4}
    first = api.post('/api/jobs', json=payload)
    assert first.status_code == 202
    job_id = first.json()['id']
    assert store.claim()['id'] == job_id
    assert store.claim() is None
    assert api.post(f'/api/jobs/{job_id}/cancel').json()['cancel_requested'] == 1
    assert store.get(job_id)['status'] == 'running'
    store.update(job_id, status='cancelled')
    queued = api.post('/api/jobs', json=payload).json()
    assert api.post(f'/api/jobs/{queued["id"]}/cancel').json()['status'] == 'cancelled'
    assert store.claim() is None
    for _ in range(8):
        assert api.post('/api/jobs', json=payload).status_code == 202
    assert api.post('/api/jobs', json=payload).status_code == 429


def test_invalid_params_and_worker_offline(client):
    api, store = client
    login(api)
    assert api.post('/api/jobs', json={'prompt': 'Valid test prompt', 'steps': 0}).status_code == 422
    assert api.post('/api/jobs', json={'prompt': 'Valid test prompt', 'image_id': '../sample.png'}).status_code == 422
    assert api.post('/api/jobs', json={'prompt': 'Valid test prompt', 'image_id': 'missing.png'}).status_code == 404
    assert api.post('/api/jobs', json={'prompt': 'Valid test prompt', 'model': 'MOVA-360p'}).status_code == 503
    store.heartbeat({'heartbeat': 0})
    assert api.post('/api/jobs', json={'prompt': 'Valid test prompt'}).status_code == 503


def test_video_range_and_missing_jobs(client):
    api, store = client
    login(api)
    assert api.get('/api/jobs/unknown').status_code == 404
    assert api.get('/api/jobs/unknown/logs').status_code == 404
    job = api.post('/api/jobs', json={'prompt': 'Valid video prompt'}).json()
    assert api.get(f'/api/jobs/{job["id"]}/video').status_code == 404
    folder = store.DATA / 'outputs' / job['id']
    folder.mkdir()
    (folder / 'video.mp4').write_bytes(b'x' * 4096)
    store.update(job['id'], status='succeeded')
    response = api.get(f'/api/jobs/{job["id"]}/video', headers={'Range': 'bytes=0-99'})
    assert response.status_code == 206
    assert len(response.content) == 100
    assert 'attachment' in api.get(f'/api/jobs/{job["id"]}/video?download=true').headers['content-disposition']

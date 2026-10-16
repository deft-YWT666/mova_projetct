import hashlib
import hmac
import io
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field

import store

PASSWORD = os.environ.get('STUDIO_PASSWORD', '')
SECRET = os.environ.get('SESSION_SECRET', '')
USERNAME = os.environ.get('STUDIO_USER', 'lj')
COOKIE = 'mova_session'
attempts = {}


@asynccontextmanager
async def lifespan(app):
    if len(PASSWORD) < 12 or len(SECRET) < 32:
        raise RuntimeError('Set STUDIO_PASSWORD (12+ characters) and SESSION_SECRET (32+ characters)')
    store.init()
    yield


app = FastAPI(title='MOVA Video Studio', version='1.0.0', lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


def authenticated(request: Request):
    cookie = request.cookies.get(COOKIE, '')
    try:
        payload, signature = cookie.rsplit('.', 1)
        expected = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature) or int(payload.split(':')[1]) < time.time():
            raise ValueError()
    except (ValueError, IndexError):
        raise HTTPException(401, '请先登录')


class Login(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=200)


@app.post('/api/login')
def login(body: Login, request: Request, response: Response):
    ip, now = request.client.host, time.time()
    failures = [stamp for stamp in attempts.get(ip, []) if stamp > now - 300]
    attempts[ip] = failures
    if len(failures) >= 10:
        raise HTTPException(429, '尝试次数过多，请 5 分钟后重试')
    if not (secrets.compare_digest(body.username.encode(), USERNAME.encode()) and
            secrets.compare_digest(body.password.encode(), PASSWORD.encode())):
        failures.append(now)
        raise HTTPException(401, '用户名或密码错误')
    attempts.pop(ip, None)
    payload = f'{secrets.token_hex(12)}:{int(now) + 86400}'
    signature = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    response.set_cookie(COOKIE, f'{payload}.{signature}', httponly=True,
                        secure=os.environ.get('COOKIE_SECURE') == 'true', samesite='strict', max_age=86400)
    return {'username': USERNAME}


@app.post('/api/logout')
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {'ok': True}


@app.get('/api/session', dependencies=[Depends(authenticated)])
def session():
    return {'username': USERNAME}


@app.get('/api/health')
def health():
    with store.connect() as db:
        db.execute('SELECT 1')
    return {'status': 'ok', 'worker_online': store.worker_info()['online']}


@app.get('/api/system', dependencies=[Depends(authenticated)])
def system():
    return {'worker': store.worker_info(), 'models': ['MOVA-360p', 'MOVA-720p'],
            'engine': 'SGLang', 'max_pending': 8}


@app.post('/api/uploads', dependencies=[Depends(authenticated)])
async def upload(file: UploadFile = File(...)):
    raw = await file.read(20 * 1024 * 1024 + 1)
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(413, '图片不能超过 20 MB')
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.width * image.height > 25_000_000:
                raise HTTPException(422, '图片尺寸过大')
            image.load()
            image_id = secrets.token_hex(16) + '.png'
            image.convert('RGB').save(store.DATA / 'uploads' / image_id, format='PNG')
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise HTTPException(422, '请上传有效的 PNG、JPEG 或 WebP 图片')
    return {'image_id': image_id, 'url': f'/api/uploads/{image_id}'}


def image_path(image_id):
    if image_id != Path(image_id).name or not image_id.endswith('.png'):
        raise HTTPException(422, '无效图片')
    path = store.DATA / 'uploads' / image_id
    if not path.is_file():
        raise HTTPException(404, '图片不存在')
    return path


@app.get('/api/uploads/{image_id}', dependencies=[Depends(authenticated)])
def uploaded_image(image_id: str):
    return FileResponse(image_path(image_id), media_type='image/png')


class Generation(BaseModel):
    prompt: str = Field(min_length=5, max_length=4000)
    negative_prompt: str = Field(default='', max_length=2000)
    image_id: str = Field(default='sample.png', pattern=r'^[a-zA-Z0-9_-]+\.png$')
    model: Literal['MOVA-360p', 'MOVA-720p'] = 'MOVA-720p'
    num_frames: Literal[49, 97, 193] = 49
    steps: int = Field(default=25, ge=4, le=50)
    seed: int = Field(default=67, ge=0, le=2147483647)


@app.post('/api/jobs', status_code=202, dependencies=[Depends(authenticated)])
def generate(body: Generation):
    image_path(body.image_id)
    worker = store.worker_info()
    if not worker['online']:
        raise HTTPException(503, 'GPU 工作进程离线，请联系管理员')
    if body.model not in worker.get('models', []):
        raise HTTPException(503, '该模型尚未就绪')
    try:
        return store.create(body.model_dump())
    except ValueError as error:
        raise HTTPException(429, str(error))


@app.get('/api/jobs', dependencies=[Depends(authenticated)])
def jobs():
    with store.connect() as db:
        return [store.unpack(row) for row in db.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT 100')]


@app.get('/api/jobs/{job_id}', dependencies=[Depends(authenticated)])
def job(job_id: str):
    result = store.get(job_id)
    if result is None:
        raise HTTPException(404, '任务不存在')
    return result


@app.post('/api/jobs/{job_id}/cancel', dependencies=[Depends(authenticated)])
def cancel(job_id: str):
    result = store.cancel(job_id)
    if result is None:
        raise HTTPException(404, '任务不存在')
    return result


@app.get('/api/jobs/{job_id}/logs', dependencies=[Depends(authenticated)])
def logs(job_id: str):
    job(job_id)
    path = store.DATA / 'logs' / f'{job_id}.log'
    if not path.exists():
        return {'text': ''}
    with path.open('rb') as stream:
        stream.seek(max(0, path.stat().st_size - 24000))
        return {'text': stream.read().decode('utf-8', 'replace')}


@app.get('/api/jobs/{job_id}/video', dependencies=[Depends(authenticated)])
def video(job_id: str, download: bool = False):
    result = job(job_id)
    path = store.DATA / 'outputs' / job_id / 'video.mp4'
    if result['status'] != 'succeeded' or not path.is_file():
        raise HTTPException(404, '视频尚未生成')
    return FileResponse(path, media_type='video/mp4', filename=f'mova-{job_id[:8]}.mp4' if download else None)


@app.middleware('http')
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['X-Frame-Options'] = 'DENY'
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


dist = Path(__file__).parent / 'dist'
if dist.is_dir():
    app.mount('/', StaticFiles(directory=dist, html=True), name='frontend')

"""Unprivileged host worker using the existing SGLang/MOVA installation."""
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / 'backend'))
import store

ROOT = Path(os.environ.get('MOVA_ROOT', '/ceph100t/lj/mova-sglang'))
# Keep the large Python environment and (when prepared) the model on local NVMe/
# tmpfs. The original worker stayed at 1% while imports and model reads waited
# on the Ceph filesystem.
cache_value = os.environ.get('MOVA_CACHE_ROOT', str(ROOT))
CACHE_ROOT = Path(cache_value)
PYTHON = os.environ.get('SGLANG_PYTHON', str(CACHE_ROOT / 'env/bin/python'))
GPUS = os.environ.get('CUDA_VISIBLE_DEVICES', '1,2,3,4,5,6,7')
def model_path(name):
    # Each model can be cached independently; a cached 720p model must not hide
    # the existing 360p installation.
    candidates = [CACHE_ROOT / 'models' / f'{name}-direct',
                  CACHE_ROOT / f'{name}-direct', ROOT / 'models' / f'{name}-direct']
    return next((path for path in candidates if (path / 'model_index.json').is_file()), candidates[-1])


MODELS = {name: model_path(name) for name in ('MOVA-360p', 'MOVA-720p')}
stopping = threading.Event()
active_process = None

# The installed SGLang public __init__ also imports its text-generation API. The
# diffusion CLI only needs SGLang's cache and Transformers compatibility setup.
BOOTSTRAP = (
    'import sys,types,pathlib; '
    'p=types.ModuleType("sglang"); '
    'p.__path__=[str(pathlib.Path(sys.prefix)/"lib/python3.12/site-packages/sglang")]; '
    'p.__package__="sglang"; sys.modules["sglang"]=p; '
    'from sglang.srt.environ import redirect_third_party_caches; '
    'redirect_third_party_caches(); '
    'from sglang.srt.utils.hf_transformers_patches import apply_all; apply_all(); '
    'from sglang.cli.main import main; main()'
)


def stop_process(process):
    if process and process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def shutdown(signum, frame):
    stopping.set()


def pulse():
    while not stopping.is_set():
        try:
            store.heartbeat({'heartbeat': time.time(), 'engine': 'SGLang', 'gpus': GPUS,
                             'models': [name for name, path in MODELS.items() if (path / 'model_index.json').is_file()]})
        except Exception as error:
            print(f'Heartbeat error: {error}', flush=True)
        stopping.wait(5)


def command_for(job):
    params = job['params']
    height, width = (352, 640) if params['model'] == 'MOVA-360p' else (720, 1280)
    outdir = store.DATA / 'outputs' / job['id']
    outdir.mkdir(parents=True, exist_ok=True)
    gpu_count = len([gpu for gpu in GPUS.split(',') if gpu.strip()])
    args = [PYTHON, '-c', BOOTSTRAP, 'generate',
            '--model-path', str(MODELS[params['model']]),
            '--num-gpus', str(gpu_count), '--sp-degree', str(gpu_count),
            '--performance-mode', os.environ.get('MOVA_PERFORMANCE_MODE', 'speed'),
            '--use-fsdp-inference', 'true', '--warmup-mode', 'off',
            # Stream T5 layers during its single encoding pass. Component CPU
            # offload alone still moves the whole 21-GiB encoder onto each GPU.
            '--layerwise-offload-components', 'text_encoder',
            '--encoder-parallel', 'replicate',
            '--prompt', params['prompt'], '--image-path', str(store.DATA / 'uploads' / params['image_id']),
            '--height', str(height), '--width', str(width),
            '--num-frames', str(params['num_frames']), '--fps', '24',
            '--num-inference-steps', str(params['steps']), '--seed', str(params['seed']),
            '--output-path', str(outdir), '--output-file-name', 'video', '--save-output']
    # K/V-gather shards sequence tokens without splitting attention heads. It
    # supports odd GPU counts and Torch SDPA on the RTX 5090 (SM12.x).
    if gpu_count > 1:
        args += ['--kv-gather-degree', str(gpu_count)]
    if params.get('negative_prompt'):
        args += ['--negative-prompt', params['negative_prompt']]
    return args, outdir


def run_job(job):
    global active_process
    job_id = job['id']
    command, outdir = command_for(job)
    environment = dict(os.environ, CUDA_VISIBLE_DEVICES=GPUS, PYTHONUNBUFFERED='1',
                       PATH=str(Path(PYTHON).parent) + os.pathsep + os.environ.get('PATH', ''),
                       HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                       PYTORCH_CUDA_ALLOC_CONF='expandable_segments:True',
                       TOKENIZERS_PARALLELISM='false')
    log_path = store.DATA / 'logs' / f'{job_id}.log'
    started, reason = time.monotonic(), None
    with log_path.open('w') as output:
        output.write('SGLang + MOVA generation\n' + json.dumps(job['params'], ensure_ascii=False) + '\n')
        output.flush()
        active_process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT,
                                          env=environment, cwd=str(ROOT), start_new_session=True)
        while active_process.poll() is None:
            if stopping.is_set():
                reason = '工作进程已停止，请重新提交任务'
                stop_process(active_process)
                break
            if store.get(job_id)['cancel_requested']:
                reason = 'cancelled'
                stop_process(active_process)
                break
            if time.monotonic() - started > int(os.environ.get('JOB_TIMEOUT', '10800')):
                reason = '生成超时（3 小时），任务已停止'
                stop_process(active_process)
                break
            with log_path.open('rb') as log:
                log.seek(max(0, log_path.stat().st_size - 12000))
                tail = re.sub(r'\x1b\[[0-9;]*m', '', log.read().decode('utf-8', 'replace'))
            steps = re.findall(r'(\d+)/(\d+)\s*\[', tail)
            if steps:
                done, total = map(int, steps[-1])
                if total == job['params']['steps']:
                    store.update(job_id, progress=min(95, 10 + int(done / total * 85)), stage=f'去噪采样 {done}/{total}')
            if '[MOVADecodingStage] started' in tail:
                store.update(job_id, progress=97, stage='编码音视频')
            stopping.wait(2)
        exit_code = active_process.wait()
        active_process = None
    if reason == 'cancelled':
        store.update(job_id, status='cancelled', stage='已取消', finished=time.time())
        return
    video = outdir / 'video.mp4'
    candidates = list(outdir.glob('*.mp4'))
    if not video.exists() and len(candidates) == 1:
        candidates[0].rename(video)
    if reason is None and exit_code == 0 and video.is_file() and video.stat().st_size > 1024:
        store.update(job_id, status='succeeded', progress=100, stage='生成完成',
                     output=str(video), finished=time.time())
    else:
        tail = log_path.read_text(errors='replace')[-1400:]
        store.update(job_id, status='failed', stage='生成失败',
                     error=reason or f'SGLang 退出码 {exit_code}。{tail}', finished=time.time())


def main():
    store.init()
    lock = (store.DATA / 'worker.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    with store.connect() as db:
        db.execute("UPDATE jobs SET status='failed',stage='工作进程重启',error='任务被中断，请重新提交',finished=? WHERE status='running'", (time.time(),))
    threading.Thread(target=pulse, daemon=True).start()
    print(f'Worker ready; GPUs={GPUS}; Python={PYTHON}', flush=True)
    while not stopping.is_set():
        job = store.claim()
        if job:
            try:
                run_job(job)
            except Exception as error:
                stop_process(active_process)
                store.update(job['id'], status='failed', stage='生成失败', error=str(error), finished=time.time())
                print(f'Job {job["id"]}: {error}', flush=True)
        else:
            stopping.wait(2)
    store.heartbeat({'heartbeat': 0, 'models': [], 'gpus': GPUS, 'engine': 'SGLang'})


if __name__ == '__main__':
    main()

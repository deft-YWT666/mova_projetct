# MOVA 视频生成工作台

基于 [Vue3-FastAPI-WebApp-template](https://github.com/Tomansion/Vue3-FastAPI-WebApp-template) 构建。Vue 3 页面使用 FastAPI 提供的 API；GPU 工作进程在宿主机运行，通过共享 SQLite 队列把生成任务交给已有的 SGLang 与 MOVA 安装。

## 获取与部署

~~~sh
git clone https://github.com/deft-YWT666/mova_projetct.git
cd mova_projetct
cp .env.example .env
# 编辑 .env，设置 STUDIO_PASSWORD、SESSION_SECRET 和宿主机用户 UID/GID
mkdir -p data
docker compose -f compose.studio.yml up -d --build
~~~

Vue 前端构建后由同一个 FastAPI 容器提供，仓库包含 Dockerfile 和 Compose 构建配置；不包含 Docker 镜像导出包。生成任务还需要宿主机上的 SGLang/MOVA 环境、模型权重和 GPU worker。部署到其他主机时，先修改 deploy/worker.service 的用户路径、模型路径和 GPU 设置，再安装为用户级 systemd 单元。

~~~sh
mkdir -p ~/.config/systemd/user
cp deploy/worker.service ~/.config/systemd/user/mova-studio-worker.service
systemctl --user daemon-reload
systemctl --user enable --now mova-studio-worker
~~~

当前服务器 GPU 0 留给既有服务，worker 使用 GPU 1–7。模型权重、真实凭据、数据库、上传图片、生成视频和运行日志不进入仓库。

本次发布以服务器源码提交 594cf917bcb671af482b2bc686fc46e7d4059de9 为基线。旧模板的 Docker Hub 自动部署及模板检查工作流未启用，也未包含在本次源码发布中。原模板 Apache 2.0 许可证及源码保留。

## 已部署的组件

- `MOVA-360p` 与 `MOVA-720p`，模型文件来自 `/ceph100t/lj/mova-sglang/models`。
- 单个网页容器提供创作页面、登录、图片上传、任务记录、取消、日志、视频预览与 MP4 下载。
- Linux 用户级 `mova-studio-worker.service` 使用 GPU 1–7 顺序运行 SGLang 推理；GPU 0 留给现有 mineru、livetalking 服务。
- 任务队列、生成结果、上传图片和运行日志保存在 `data/`。

## 访问

- 工作台：`http://221.5.60.142:18088`
- 当前电脑的 SSH 隧道入口：`http://127.0.0.1:18089/`，需要保持隧道运行。
- 健康检查：`http://221.5.60.142:18088/api/health`
- 默认用户名：`lj`
- 部署密码保存在服务器 `/home/lj/video-studio/.env`。

## 运维

```sh
cd /home/lj/video-studio
docker compose -f compose.studio.yml ps
docker compose -f compose.studio.yml logs -f web
systemctl --user status mova-studio-worker
```

更新页面与 API：

```sh
cd /home/lj/video-studio
docker compose -f compose.studio.yml up -d --build
```

SGLang 模型路径和 GPU 可由 `deploy/worker.service` 中的环境变量调整；部署位置为 `~/.config/systemd/user/mova-studio-worker.service`，`.worker.env` 可覆盖环境变量。

## 多卡推理配置

- 复用原安装的 SGLang、MOVA 权重，本地 Python 环境为 `/home/lj/.local/share/mova-studio/sglang-env`，避免从 Ceph 导入大量 Python 文件。
- `CUDA_VISIBLE_DEVICES=1,2,3,4,5,6,7`；7 卡 FSDP 分片与 KV gather 序列并行，`performance-mode=speed`，文本编码器逐层 offload。
- RTX 5090 使用 Torch SDPA；KV gather 支持 7 卡，不要求 attention heads 能被 7 整除。
- MOVA 的 stage 直接调用 DiT 子模块，原实现绕过 FSDP root hooks。补丁通过 `register_fsdp_forward_method` 恢复 root hooks，避免 Tensor/DTensor 混用和多份通信缓冲。

重新准备推理环境时执行（幂等，原文件自动备份）：

```sh
cd /home/lj/video-studio
python3 scripts/apply-mova-fsdp-patch.py \
  --site-packages /home/lj/.local/share/mova-studio/sglang-env/lib/python3.12/site-packages
```

720p 权重缓存位于 `/dev/shm/lj-mova-studio/MOVA-720p-direct`。每个模型分别选择缓存，未缓存的 360p 使用原安装目录。`/dev/shm` 在服务器重启后清空；重启后应先从原模型目录重新准备缓存，否则会回退 Ceph，加载可能再次变慢。当前每个任务独立启动推理进程，仍有约一分钟的加载开销。

## 验证

后端 5 项测试通过；登录、图片上传、任务提交、进度轮询、视频下载均已用真实 MOVA-720p 任务验证。720p / 49 帧 / 25 步实测 612.92 秒；部署持久环境、重启 worker 后的 4 步任务实测 220.26 秒。两次视频均完整解码通过，25 步结果可在网页任务记录预览和下载。详细证据见服务器 `/home/lj/video-studio/inspection/2026-10-15-service-check.md`。

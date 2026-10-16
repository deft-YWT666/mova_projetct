FROM node:22-slim AS build-stage
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ .
RUN npm run build

FROM python:3.12-slim
ARG PIP_INDEX_URL=https://pypi.org/simple
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
COPY backend/requirements-studio.txt ./requirements.txt
RUN pip install --no-cache-dir --index-url ${PIP_INDEX_URL} -r requirements.txt
COPY backend/studio.py backend/store.py ./
COPY --from=build-stage /frontend/dist dist
EXPOSE 3000
CMD ["uvicorn", "studio:app", "--host", "0.0.0.0", "--port", "3000"]

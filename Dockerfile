# kkari-AI 서버 이미지. 빌드 컨텍스트 = 이 레포 루트 (docker build -t kkari-ai:local .)
# - torch 는 CPU 전용 휠을 쓴다 (uv.lock 의 기본 휠은 CUDA/nvidia 패키지가 딸려 와 수 GB 가 된다)
# - 임베딩 모델(약 470MB)은 빌드 때 받아서 이미지에 넣는다 (기동 때 인터넷 의존 제거)
FROM python:3.11-slim AS reqs
RUN pip install --no-cache-dir uv
WORKDIR /w
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-hashes --no-dev --no-emit-project -o all.txt \
 && grep -vE '^(torch|triton|nvidia-)' all.txt > req.txt

FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY --from=reqs /w/req.txt /tmp/req.txt
RUN pip install "torch==2.15.0" --index-url https://download.pytorch.org/whl/cpu \
 && pip install -r /tmp/req.txt

COPY *.py preview.html ./
COPY data data
RUN python download_model.py

RUN useradd --system --uid 1001 --create-home kkari \
 && mkdir -p data/intake_logs data/consult_logs && chown -R kkari:kkari data/intake_logs data/consult_logs
USER kkari

EXPOSE 8000
# --http h11: 백엔드(JDK HttpClient)가 보내는 Upgrade: h2c 요청의 본문을 httptools 구현은 버려서 422 가 난다
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--http", "h11"]

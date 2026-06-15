FROM python:3.11-slim

WORKDIR /app

# v3.9.38 · 容器时区统一到北京时间（UTC+8）
# 之前容器是 UTC，导致 datetime.now() / datetime.fromtimestamp() 全部偏 8h，
# 历史报告时间显示 04:20 而非 12:20、行为分析把 21:00 当成 13:00。
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 安装系统依赖（Playwright Chromium 所需）
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    wget \
    unzip \
    fontconfig \
    fonts-wqy-microhei \
    fonts-noto-cjk \
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

# 刷新字体缓存
RUN fc-cache -fv

# 先复制依赖清单，最大化层缓存命中
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目文件（.dockerignore 排除大文件/缓存）
COPY . .

# 安装 Playwright Chromium（自带系统依赖）
RUN python -m playwright install chromium

# 持久化目录
RUN mkdir -p /app/reports /app/.source_cache

# 暴露端口（web_app.py 默认 5000）
EXPOSE 5000

# 启动 web_app（-u 关闭 stdout 缓冲，方便 docker logs 实时看）
CMD ["python", "-u", "web_app.py"]

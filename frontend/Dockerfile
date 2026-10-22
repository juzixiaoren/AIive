# ── 构建阶段：Node 编译前端静态资源 ──
FROM node:20-alpine AS build
WORKDIR /app

# npm 源可配置：默认腾讯云镜像（国内），CI 用 build-arg 切官方源。
ARG NPM_REGISTRY=https://mirrors.cloud.tencent.com/npm/
RUN npm config set registry "$NPM_REGISTRY"

COPY frontend/package.json ./
RUN npm install
COPY frontend ./
RUN npm run build

# ── 运行阶段：nginx 托管静态文件并反代 API/WS ──
FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 8080

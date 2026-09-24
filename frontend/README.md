# SAYACODE Web 界面

本目录只包含浏览器界面。Agent 执行、审批恢复、会话历史、任务与记忆都由 Python 宿主持有；前端通过 `src/api/` 中的类型化 HTTP/SSE 客户端读取投影并提交明确操作。

## 开发与构建

先启动固定端口的本机宿主：

```powershell
sayacode --no-open --port 8765
```

另开终端运行前端：

```powershell
cd frontend
npm ci
npm run dev
npm test
npm run build
```

开发服务器将 `/api` 转发到本机 `127.0.0.1:8765`，因此开发时需要按上面的命令固定服务端口。普通 `sayacode` 启动会自动选择空闲端口；生产界面不连接 Vite。`npm run build` 包含 TypeScript 检查，将静态资源写入 `src/sayacode/web/static/`，由 Python wheel 和 sdist 一同分发。发布时提交构建产物，使从 Git 源码安装也不依赖 Node。

## 状态与事件边界

- 工作区、会话和任务目录通过 API 获取；消息、待办和中断从线程快照获取。浏览器不维护第二份持久会话历史。
- SSE 按工作区订阅，每条事件带 `instance_id:seq` 复合游标和所属线程。失去缓冲时先重新读取快照，再从新游标订阅。
- 浏览器关闭或刷新只断开观察连接，不持有 Agent 执行任务。
- 启动令牌由 URL fragment 交换为 HttpOnly Cookie 和 CSRF token。修改请求统一携带 `X-CSRF-Token`；前端不存储模型密钥。
- `src/products/` 的每件操作都有对应领域端点。生产构建没有演示数据回退。

三栏分别负责工作区与会话、当前 Agent 的对话与轨迹、协作/审批/交付检查器。窄屏将侧栏变成可关闭的抽屉；键盘焦点和审批对话框由原生语义及 Radix 原语管理。

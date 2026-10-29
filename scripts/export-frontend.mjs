import { readFile, readdir, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = fileURLToPath(new URL("../", import.meta.url));
const files = [
  ["app/views/index.html", "html"], ["app/views/css/styles.css", "css"],
  ["app/views/js/api.js", "javascript"], ["app/views/js/sse.js", "javascript"],
  ["app/views/js/audio.js", "javascript"], ["app/views/js/app.js", "javascript"],
  ["app/services/auth.py", "python"], ["app/controllers/auth_controller.py", "python"],
  ["app/services/registry.py", "python"], ["app/main.py", "python"],
  ["app/controllers/chat_controller.py", "python"],
  ["package.json", "json"], ["package-lock.json", "json"], ["scripts/build-frontend.mjs", "javascript"],
  ["scripts/generate-placeholder.ps1", "powershell"], ["scripts/run_frontend_preview.py", "python"],
  ["app/views/assets/styles.css", "css"], ["app/views/assets/icons.css", "css"],
  ["app/views/assets/eventsource-parser.js", "javascript"],
  ["app/views/assets/lucide-license.txt", "text"],
  ["app/views/assets/eventsource-parser-license.txt", "text"],
  ["app/views/assets/tailwind-license.txt", "text"],
];
for (const file of await readdir(path.join(root, "app/views/assets/icons"))) files.push(["app/views/assets/icons/" + file, "svg"]);
let document = `# 前端交付说明

## 设计思路

配色以暖白 #f7f8f6、石墨灰 #303531 和低饱和绿色 #376657 为主，玫瑰色仅用于头像、提醒等小面积细节。
正文采用中文系统无衬线字体，品牌名与记忆页标题用宋体系列形成轻微对比；不加载外部字体。
桌面采用左侧导航与宽主区，聊天、世界书、状态、时间轴、画像和体检分别组织；手机采用带焦点约束的抽屉导航。
工具使用本地 Lucide 图标和工具提示；反馈、命令使用原生 dialog；请求中有禁用与忙碌状态，回复可停止。
界面保留充足留白、8px 圆角、轻微阴影和短过渡，无循环装饰动画，支持减少动态效果的系统设置。

## 分文件完整代码

以下内容由项目中的实际文件导出。每个代码块上方标注文件名；前端、鉴权后端、运行资源、编译脚本与占位图片均已落入项目。代码在现有项目内运行，继续使用项目的 requirements.txt 和其余后端模块。
`;
const usage = `
## 使用方法

本项目已包含编译后的静态资源。原数据服务启动后可打开 http://127.0.0.1:8765；修改前端后刷新浏览器，更新后端代码后需要重启服务。

登录后点击侧栏左上角“旧识”可返回对话并聚焦输入框；手机端会自动收起导航，已有聊天、草稿和登录状态保留。按钮支持回车和空格操作，悬停时显示“返回对话”。

\`\`\`powershell
# 使用已安装依赖的 Python 解释器
& 'python' web.py
\`\`\`

新部署首次创建账号时，自行设置 8 至 128 个字符的密码，并保留“关联我原来的世界书、时间轴和画像”选项。账号通过 data_uid 关联根目录的原默认用户数据，原文件不会移动或覆盖；已有独立记忆的账号不能直接改绑。创建后刷新 8765 页面即可用账号密码登录。
后续输入账号和密码即可登录：浏览器提交 /api/auth/login，后端验证成功后签发令牌，浏览器自动保存并在所有业务请求中携带 Authorization: Bearer。无需手动获取或填写令牌，也不会保存密码。刷新页面通过 /api/auth/me 验证真实账号；退出调用 /api/auth/logout 吊销当前会话。
密码以随机盐 PBKDF2-HMAC-SHA256（600000 次迭代）保存，令牌仅在后端保存 SHA256 摘要。登录会话有效期为 7 天，到期后重新登录。创建密码账号后，业务 API 自动要求有效令牌；对外部署仍需设置 WB_AUTH_REQUIRED=1。
首次账号创建只允许本机或管理员。已设置 WB_ADMIN_TOKEN 时，用下面的终端命令初始化；不开放匿名公众注册。旧管理令牌保持兼容，但不会自动为账号设置密码。
旧进程缺少 /api/healthz 的 password_login 能力字段时，页面会提示重启后端。
独立预览可通过 python scripts/run_frontend_preview.py --port 8768 --data-dir _tmptest/password-auth-preview 启动，数据使用测试目录，用于检查界面与登录流程。查看原有记忆请使用 8765 正式服务。

\`\`\`powershell
# 管理员初始化并关联原数据（交互输入密码）
& 'python' -m app.services.auth account demo --inherit-default
# 创建额外账号，每个账号使用独立数据目录
& 'python' -m app.services.auth account other
# 忘记密码时在本机终端重置；旧令牌全部吊销，记忆关联保留
& 'python' -m app.services.auth password demo
\`\`\`

\`\`\`powershell
# 修改 Tailwind 类或样式后执行
npm ci
npm run build:frontend
# 检查请求、身份、SSE 和语音回退
npm run test:frontend
& 'python' tests/test_password_auth.py
& 'python' tests/test_identity_transport.py
& 'python' tests/test_frontend_contract.py
\`\`\`

占位头像是项目内的 PNG，可直接替换 app/views/assets/avatar-placeholder.png。图标、SSE 解析器与 Tailwind 的许可证保留在 assets 中。

## 可优化点

- 替换占位头像，增加正式品牌资产。
- 后端提供结构化时间轴、状态与历史对话接口，减少文本报告的展示限制。
- 增加管理员账号管理页面，当前额外账号创建与密码重置通过终端完成。
- 对外部署时配置 HTTPS，并按需要扩展多实例共享的登录限流。

## 验证范围

截图和流程报告位于 _tmptest/frontend-screenshots/report.json。浏览器流程使用符合后端格式的 API 测试数据，静态资源来自实际 FastAPI 预览服务。
报告记录 22 项流程检查和 19 个视口截图，包含账号密码登录、首次创建与旧记忆选择、密码确认与可见切换、错误密码、自动恢复、退出、401 恢复、URL 令牌迁移、旧后端提示、世界书增删、反馈、命令、静音及 1350/820/375/320px 布局；额外覆盖首次创建后状态加载失败和另一标签页完成创建后的恢复，以及品牌按钮的桌面点击、回车与空格操作、手机收起导航、聊天内容与草稿及会话保留。所有视口无横向溢出或图片加载失败。
另在真实隔离 FastAPI 服务运行 tests/password-api-check.mjs，_tmptest/password-api-report.json 记录创建与鉴权、默认数据关联、密码校验与令牌签发、退出吊销四项检查全部通过；临时测试账号已清理。后端密码测试 13 项、身份传输测试 6 项、前端测试 12 项通过；纯函数套件和资源契约检查通过。并发回归验证重置密码后，正在验证旧密码的登录不能补发有效令牌。
验证使用隔离的测试数据，不读取也不写入正式服务的记忆库。
语音分包与 MSE 失败回退通过单元验证，鉴权与浏览器检查未发送真实模型对话或实听真实 TTS。

`;
for (const [file, language] of files) {
  document += `\n### ${file}\n\n\`\`\`${language}\n${(await readFile(path.join(root, file), "utf8")).trimEnd()}\n\`\`\`\n`;
}
document += "\n### app/views/assets/avatar-placeholder.png\n\n![头像占位图](app/views/assets/avatar-placeholder.png)\n";
document += usage;
await writeFile(path.join(root, "docs/前端交付说明.md"), document);
console.log(`frontend delivery exported (${files.length} complete source and asset files)`);

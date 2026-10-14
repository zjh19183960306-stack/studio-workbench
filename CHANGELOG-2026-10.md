# CHANGELOG 2026-10（近两天工作台 / 出片）

时区均为 Asia/Shanghai。只记代码与运维事实，不含用户媒体或提示词内容。

## 2026-10-14 — 迁移到 939 + 自监督一批

### 迁移
- 租 939（西北B），无卡开机，数据盘扩到 **150G**（再扩 250G 失败）
- 用户决定：**只搬工作台和出片**；换脸栈留 945 并关机保留
- 控制台跨实例拷遇每日限额 → SSH tar 管道；`sparse_bak` 撑盘后停止并在 939 删除
- 945 关机留换脸；939 挂卡后起 studio / Comfy / Serveo（`bbstudio6006b`）
- 关掉 939 空闲自动关机；公网曾关密码保护（Serveo 加载即断）

### H3 / worker（自监督条目节选）
- **08:09 cn_end→1.0** 后改为社区偏好：深度路径默认 **0.75**（10:08），非 depth 仍 1.0；`H3_CN_END` 可覆盖；`render_meta` 同步 cn_*（10:45）
- **分段**：min/max 帧钳制（约 5–15s）；短尾滑动；拼接后按片源裁切；xfade 失败硬拼去重叠（08:44 / 09:13 / 09:40）
- **取消**：interrupt → queue clear+interrupt → **job 级** `/api/jobs/{id}/cancel` + 带 prompt_id interrupt（11:11 / 11:44 / 12:13）
- **盘**：`disk_preflight`（约 6G 下限）；`scrub_stale_h3_scratch`；独立 `watch_h3_scrub.sh`；autostart 挂钩（13:12 / 13:46 / 14:09 / 14:20）
- 部署原则：GPU 队列非空时不重启正在跑的 worker；scrub watcher 可单独挂

### 运维入口
- SSH 939：` -p 42309` + `autodl_ed25519`
- 公网：`https://bbstudio6006b.serveousercontent.com`
- 办公室台式私网：SSH `-L 0.0.0.0:6006:127.0.0.1:6006`（计划任务名曾用 `autodl-tunnel-6006`；若权限不足则 Startup 文件夹兜底）

## 2026-10-12–13（摘要）
- 模块线：MiniMax H3 装机、深度提取、工作台一键出片 UI
- 视频库：封面、上传、抖音链接扒、历史成品分区、删除、贴原声
- 深度：最好模型 + 整段；出片只用深度停用白模
- 默认分辨率约 1080×1920；时长跟原片；超长按约 60s 分段再拼（后续钳制到官方窗）
- 公网 Serveo 换 `bbstudio6006b`；未登录禁止出图（后因 Serveo 断连临时关密码，见上）
- 认证图 / 暗存加强 / UI 狂刷卡顿等多轮修复

## 未做 / 未验
- 多项 GPU 冒烟见 `docs/GPU_VERIFY_CHECKLIST.md`
- 945 换脸带卡验收
- 盘扩到 250G
- Mac 私网在 Mac 离线时未重验

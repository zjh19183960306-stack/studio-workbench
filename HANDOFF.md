# 工作交接 HANDOFF（峻弘 AutoDL 工作台 / 出片）

更新时间：2026-10-14（Asia/Shanghai）  
仓库：https://github.com/zjh19183960306-stack/studio-workbench  
用途：给另一个账号登录后快速了解这两天做了什么、怎么开、哪些别碰。

---

## 1. 最终目标（Ultimate goal）

本地部署一套「工作台」：

1. 上传参考视频 → 提取深度驱动（优先 VDA 深度；白模线已停用）
2. 选人物参考图（服装只来自人物图，不另开提示词造衣服）
3. 一键 H3 出片（Fun ControlNet + MiniMax H3），默认交付约 1080×1920，贴原声
4. 公网 / 家里 Wi‑Fi / 办公室台式 都能打开同一套工作台
5. **换脸栈留在旧机**，不跟工作台绑死在同一台新机上

隐私硬规则（对所有接手的人同样生效）：

- **禁止**打开 / 下载 / 提交：用户图片、视频、成品、材料、`prompt_overrides.json`、自定义提示词、token、密码、`studio_token.txt`、SSH 私钥、模型权重
- 只报告：数量、文件名、错误码、HTTP 状态；只提交代码 / 脚本 / 非密钥配置 / 本交接文档

---

## 2. 架构（Architecture）

| 组件 | 作用 | 所在 |
|------|------|------|
| 工作台 studio（Flask/静态 UI，端口 6006） | 上传、勾选、出片队列、视频库、历史成品 | **939 机** `/root/autodl-tmp/studio` |
| ComfyUI（端口 8192） | H3 / 深度等推理 | 939 |
| `motion_h3_worker.py` | 出片队列 worker（Fun CN depth、分段、拼接、取消、盘空间清理） | 939（studio 旁或 autodl-tmp） |
| Serveo 公网隧道 | `https://bbstudio6006b.serveousercontent.com` → 6006 | 939 `tunnel_loop` / `watch_serveo` |
| 台式 SSH 本地转发 | `http://<台式LAN>:6006` → 939:6006 | Windows 计划任务 `autodl-tunnel-6006`（若权限不足则用 Startup：`autodl-tunnel-6006-loop.bat`） |
| 换脸（DreamID-V / DeepFaceLab / FaceFusion / faceswap-package-cache） | **仍在旧 945，关机保留** | 945 `/root/autodl-tmp/...`（未迁） |

数据盘现状（2026-10-14 下午）：939 数据盘 **150G**（扩 250G 失败，宿主机满），工作台+出片已迁；换脸未迁。出长片前要腾空间（曾只剩约 16–30G）。

---

## 3. 机器与 SSH

### 新机 939（工作台 + 出片，当前主用）

- 实例：`51014e9056-7b16c52d`，西北B区
- SSH：`ssh -i %USERPROFILE%\.ssh\autodl_ed25519 -p 42309 root@connect.westd.seetacloud.com`
- 数据盘：150G，镜像 PyTorch 2.8 / CUDA 12.8
- 挂卡请用 AutoDL **网页控制台**（不要用 API）
- 云机 PATH：`/usr/sbin:/usr/bin:/sbin:/bin`；Python：`/root/miniconda3/bin/python`

### 旧机 945（换脸保留，已按用户要求关机）

- 实例：`33ac4c8efc-9187cdac`
- SSH 端口曾用：`26958`（关机后不可达）
- 磁盘约 255G；换脸套件原样留着
- 关机后硬盘可能仍按天计费

### 中继电脑

- Windows 台式 `WIN-9HI0CKL795O`（machineId `54f99e50-7da2-45b9-9163-70c433fdf8f5`）：办公室私网隧道 + 往云机 scp/ssh
- Mac：曾做过 Wi‑Fi 私网；离线时不要动。若仍指向 42309，开机后再核一次

---

## 4. 怎么启动工作台（How to start）

在 939 上（有卡时）：

```bash
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
cd /root/autodl-tmp
bash autostart.sh          # 或 start_studio.sh + watch_* 脚本
# 期望：6006 studio、8192 Comfy、H3 worker、serveo/tunnel、watch_h3_scrub
```

自检（只看状态码，不读内容）：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:6006/
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8192/
```

公网：https://bbstudio6006b.serveousercontent.com  
办公室台式私网：`http://192.168.1.200:6006`（2026-10-14 办公室 WLAN 已验证 HTTP 200；环回 `http://127.0.0.1:6006` 亦 200）  
本机环回：`http://127.0.0.1:6006`（隧道起来后）

空闲自动关机：已在 939 关掉，避免再「刚迁完就自己关」。

用户曾要求取消公网密码保护（Serveo 一加载就断）——接手时确认是否仍 `NO_TOKEN` / 无登录门；**私网仍建议以后加回鉴权**。

---

## 5. 迁移摘要 945 → 939

用户原话脉络：无卡开机 → 扩盘 →「只搬工作台和出片」→ 旧机关机留换脸。

已到 939（约）：ComfyUI、models、wan_animate、videos、depth_anything、vda、motion_previews、studio、char_anim_h3（sparse_bak 大文件曾撑爆盘，已在 939 删除 part）、outputs/inputs/thumbs 等出片相关。

**未迁**：dreamidv-deploy、deepfacelab、facefusion、faceswap-package-cache（留 945）。

跨实例拷曾受：源机需关机、约 160MB/s、每日 10 次限额用尽 → 改 SSH tar 管道；Windows 中继太慢时改控制台内拷。

---

## 6. 近两天自监督 / 代码改动（详见 CHANGELOG-2026-10.md）

| 项 | 要点 |
|----|------|
| cn_end | 深度路径默认 `end_percent=0.75`；非 depth 1.0；可用 `H3_CN_END` 覆盖 |
| 取消 | job 级 `POST /api/jobs/{id}/cancel` + 带 `prompt_id` 的 `/interrupt` + queue clear |
| 盘空间 | `disk_preflight`（默认最少约 6G）；`scrub_stale_h3_scratch`；独立 `watch_h3_scrub.sh`（不打断正在跑的 GPU 任务） |
| 分段 | 官方约 5–15s 窗；短尾滑动；拼接后按片源时长裁切；xfade 失败则硬拼并裁重叠 |
| 多参考 | 多图一步；`<Picture i>` + 角色分工；steps 1ref=20 / 多 ref=25 |
| NO_TOKEN | 公网曾关密码保护以解决 Serveo 断连 |
| idle_shutdown | 939 关闭空闲自动关机 |
| 部署注意 | 改 worker 后若 GPU 队列非空，**不要强杀**当前 H3/Comfy；scrub watcher 可独立挂 |

GPU 挂卡验收清单：见 `docs/GPU_VERIFY_CHECKLIST.md`（多项仍标记「需带卡冒烟」，取消 / scrub / cn_end meta 等）。

---

## 7. 打开待办（Open todos，接手可接着做）

- 带卡完整冒烟：取消中途停、scrub 不误删成片、`render_meta.cn_end` 与图一致
- 出长片前腾盘（150G 上限）
- 参考照整批只分析一次；角色 LoRA 环境盘查
- 工作台验收：大图续传 + 只改动作手掌 + 锁衣服
- 动作预演封面+人物小图；勾选可见汇总+出片前确认
- 上传一直重试的问题
- 945 换脸栈：用户要求先关机留着；以后挂卡再验 FaceFusion/DeepFaceLab
- Mac 私网：开机后再对齐端口 42309
- 两个大视频迁移后续传（若仍挂起）

---

## 8. 本仓库包含 / 排除

**包含**：studio 代码与脚本（无媒体）、autostart/watch/tunnel 脚本、H3 worker 与相关 json/清单、本 HANDOFF、CHANGELOG、GPU_VERIFY_CHECKLIST。

**排除**：任何图片/视频/缩略图/成片、模型权重、`prompt_overrides.json`、token、密码、SSH 密钥、用户材料。

---

## 9. 给另一个 GitHub 账号的阅读顺序

1. 读本文件  
2. 读 `CHANGELOG-2026-10.md`  
3. 读 `docs/GPU_VERIFY_CHECKLIST.md`  
4. 看 `scripts/` 与 `h3/motion_h3_worker*.py` 的取消 / scrub / cn_end 段落  
5. **不要**去云机翻 outputs / inputs / prompt 文件  

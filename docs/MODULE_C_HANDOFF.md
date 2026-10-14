# MODULE C 接线合同

本模块不安装模型，不修改 char_anim_worker / faceswap_remote_gate。

## MODULE B → 动作预演

真实预演目前使用 waiting_gpu stub，没有部署第二个 SSH/推理 worker。可由 B 的调度器消费 studio/motion_preview_queue 中任务，必须遵守 owner、video_id 与 make_depth/make_white。视频路径仅在可信后端调用 motion_preview.catalog() 映射，不向 API 暴露。

输出固定存 studio/outputs/motion_preview/<job_id>/result-depth.mp4 或 result-white.mp4。确认视频编解码有效、无声音、无 RGB 原片后，再原子写回任务：state=done，results 为以下对象数组：

```
{"id":"<job_id>:depth","kind":"depth","slot":"result-depth.mp4","url":"/api/motion-preview/jobs/<job_id>/results/result-depth.mp4"}
{"id":"<job_id>:white","kind":"white","slot":"result-white.mp4","url":"/api/motion-preview/jobs/<job_id>/results/result-white.mp4"}
```

白模若采用 B 的 gray_sil，应在 message 明确写“灰剪影降级，非精确人体白模”。不可把合成预览作为真实动作提取。预览截断需中文说明。当前产品接口没有 synthetic-test 开关；仅 operator 自测脚本会暂时注入明确标注的合成结果。

## MODULE C → MODULE A H3

studio/motion_generate_queue 是独立持久队列。每个任务包含 owner、image_ids、motion_ids/motion_preview_ids、control_kind、anti_bleed（默认 true）、engine=minimax_h3_ref2va、state=engine_pending。

A 可在自己的显卡互斥调度器中调用 `motion_h3_bridge.dispatch_one(adapter)`；不要启动第二个推理主循环。adapter 必须提供：

- probe()：只有 gpu=true、ready=true、idle=true、control_video_input=true、engine=minimax_h3_ref2va_local 全部满足才可交接。
- enqueue(manifest)：幂等持久入队，以 manifest.id 去重；成功返回 accepted=true。manifest.images 为人物图 id/path；manifest.controls 为通过预览 id/kind/path；anti_bleed 原值保留。appearance_source=images_only、motion_source=approved_controls_only。

钩子不提供原参考 RGB 路径，不调用云 API，不复制大文件；对已提取控制视频不能再次按原始 RGB 做动作提取。旧 char_anim 入口当前接 video_id 并提取骨架，且其未就绪分支可能 failed，因此本模块没有直接调用旧入口。

probe 必须验证真实本地 H3 readiness；勿以 stub 或文件存在冒充引擎就绪。桥接钩子没有自动注册到 A worker，目前等待 A 接入；等待任务不会自行开始实际 H3 推理。

完成后 A 用 worker_common.atomic 更新同一任务状态，产物放 studio/outputs/motion_generate/<job_id>/result-N.mp4，output_slots 写固定槽，state=done，results 写本模块受登录保护的 /api/motion-preview/generation-jobs/<job_id>/results/result-N.mp4。保留 owner。API 根据 owner/state/slot 再校验。不能给 UI 返回直接磁盘路径或不带 cookie 的公开 URL。

## 验证

operator 自测脚本 `/tmp/module_c_selftest.py`：使用合成 1 秒小视频和 64×64 色块，接口全流程、H3 hook 模拟门禁、所有 cookie/owner 门禁验证后删除合成输入、输出和任务。不会读取真实媒体或调任何真实推理。

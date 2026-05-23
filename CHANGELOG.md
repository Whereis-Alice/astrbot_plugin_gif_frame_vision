# 更新日志

## 0.7.2 - 2026-05-23

- 将 `README.md`、`CHANGELOG.md` 和插件元数据改回中文
- 保留 fork 来源说明，方便后续通过你的仓库直接更新

## 0.7.1 - 2026-05-23

- 将插件元数据和仓库地址切换到 `Whereis-Alice` 的 fork，便于后续直接更新
- 在 `README.md` 中补充上游来源和当前维护分支说明
- 新增 `CHANGELOG.md`

## 0.7.0 - 2026-05-23

- 新增 `_conf_schema.json`，让抽帧策略、提示策略和清理策略可以直接在 AstrBot 中配置
- 重构插件配置读取逻辑，移除主要硬编码策略
- 修复 GIF 抽帧时首帧覆盖源文件导致后续帧丢失的问题
- 新增一次请求中处理多个 GIF 附件的支持
- 提示注入新增 `extra_user_content`、`prompt`、`system_prompt` 三种位置
- 更新插件文档和元数据，补齐 AstrBot 兼容性说明

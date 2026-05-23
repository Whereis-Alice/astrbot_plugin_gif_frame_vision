# GIF Vision Helper

这是一个 AstrBot 插件，用于在视觉模型收到请求前，将本地 GIF 动图抽样为多帧 JPEG 静态图，帮助模型更准确地理解动作、表情和场景变化。

## 插件来源

- 上游原仓库：[Yanlyn/astrbot_plugin_gif_vision_helper](https://github.com/Yanlyn/astrbot_plugin_gif_vision_helper)
- 当前维护分支：[Whereis-Alice/astrbot_plugin_gif_vision_helper](https://github.com/Whereis-Alice/astrbot_plugin_gif_vision_helper)

这个 fork 以原作者实现为基础，补充了可配置策略、提示词策略和一些运行时修复，方便后续直接通过 Git 更新。

## 功能特性

- 通过文件头识别本地 GIF，不依赖扩展名
- 将 GIF 抽样为多帧 JPEG，并重写 `image_urls`
- 支持在一次请求中同时处理多个 GIF
- 支持将 GIF 说明注入到 `extra_user_content`、`prompt` 或 `system_prompt`
- 自动清理插件生成的临时抽帧文件

## 配置说明

插件通过 `_conf_schema.json` 暴露三组配置：

- `sampling_policy`：抽帧数量、缩放尺寸、JPEG 质量、大文件降档阈值
- `hint_policy`：提示注入开关、注入位置、提示模板
- `cleanup_policy`：临时文件保留时长

默认抽帧策略基本保持和旧版本一致：

- 总帧数 `<= 4` 时抽 `3` 帧
- 总帧数 `<= 8` 时抽 `4` 帧
- 总帧数 `<= 16` 时抽 `5` 帧
- 总帧数 `> 16` 时抽 `6` 帧
- 文件体积超过 `5 MB` 或 `10 MB` 时会自动减少抽帧数

## 安装方式

1. 将本插件目录放到 `data/plugins/astrbot_plugin_gif_vision_helper`
2. 通过 `pip install -r requirements.txt` 安装依赖
3. 重载插件或重启 AstrBot
4. 在插件配置面板中按需调整策略

## 兼容性

- AstrBot `>=4.16,<5`
- 已按 AstrBot `4.25.0` 版本做过交叉验证

## 更新日志

详见 [CHANGELOG.md](./CHANGELOG.md)

# GIF Vision Helper

AstrBot plugin for sampling local GIFs into JPEG frames before a vision model sees them.

## Source

- Original upstream: [Yanlyn/astrbot_plugin_gif_vision_helper](https://github.com/Yanlyn/astrbot_plugin_gif_vision_helper)
- Maintained fork: [Whereis-Alice/astrbot_plugin_gif_vision_helper](https://github.com/Whereis-Alice/astrbot_plugin_gif_vision_helper)

This fork keeps upstream behavior as the base and adds configurable sampling strategy, configurable hint strategy, and a few runtime fixes for easier long-term maintenance.

## Features

- Detects local GIFs by file header, not file extension
- Converts GIFs into sampled JPEG frames and rewrites `image_urls`
- Handles multiple GIFs in one request
- Injects a GIF hint into `extra_user_content`, `prompt`, or `system_prompt`
- Cleans up generated temp frames automatically

## Config

`_conf_schema.json` exposes three groups:

- `sampling_policy`: frame count, resize, JPEG quality, size penalties
- `hint_policy`: hint toggle, injection target, hint templates
- `cleanup_policy`: temp-file retention

Default sampling stays close to the old behavior:

- `<= 4` frames -> sample `3`
- `<= 8` frames -> sample `4`
- `<= 16` frames -> sample `5`
- `> 16` frames -> sample `6`
- Large files above `5 MB` or `10 MB` get fewer sampled frames

## Install

1. Put this folder under `data/plugins/astrbot_plugin_gif_vision_helper`
2. Install dependencies with `pip install -r requirements.txt`
3. Reload the plugin or restart AstrBot
4. Adjust the strategy in the plugin config UI

## Compatibility

- AstrBot `>=4.16,<5`
- Verified against AstrBot `4.25.0`

## Changelog

See [CHANGELOG.md](./CHANGELOG.md).

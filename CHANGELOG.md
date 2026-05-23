# Changelog

## 0.7.1 - 2026-05-23

- Switched plugin metadata and repository URL to the `Whereis-Alice` fork for future updates
- Added source attribution in `README.md`
- Added this changelog file

## 0.7.0 - 2026-05-23

- Added `_conf_schema.json` so sampling strategy, hint strategy, and cleanup policy are configurable in AstrBot
- Refactored the plugin to load strategy settings from config instead of hardcoded constants
- Fixed GIF frame extraction so later frames are not lost after overwriting the first frame
- Added support for handling multiple GIF attachments in one multimodal request
- Changed hint injection to support `extra_user_content`, `prompt`, and `system_prompt`
- Updated plugin docs and metadata for AstrBot compatibility

"""
AstrBot plugin: GIF Vision Helper.

Convert local GIF attachments into sampled JPEG frames before a vision model
receives them, so the model can reason about motion instead of a single still.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from PIL import Image, UnidentifiedImageError
from PIL.Image import DecompressionBombError

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.message import TextPart


PLUGIN_ID = "astrbot_plugin_gif_vision_helper"
PLUGIN_VERSION = "0.7.2"
PLUGIN_DESC = "\u5c06 QQ GIF \u52a8\u56fe\u62c6\u6210\u591a\u5e27\u9759\u6001\u56fe\uff0c\u8ba9\u591a\u6a21\u6001\u6a21\u578b\u66f4\u7a33\u5b9a\u5730\u7406\u89e3\u52a8\u6001\u5185\u5bb9"
PLUGIN_REPO = "https://github.com/Whereis-Alice/astrbot_plugin_gif_vision_helper"

MB = 1024 * 1024
DEFAULT_MULTI_HINT = (
    "[\u7cfb\u7edf\u63d0\u793a] \u672c\u6b21\u7528\u6237\u53d1\u9001\u4e86 {gif_count} \u4e2a GIF \u52a8\u56fe\uff0c"
    "\u63d2\u4ef6\u5df2\u5c06\u5176\u62bd\u5e27\u4e3a {frame_count} \u5f20\u9759\u6001\u56fe\u3002"
    "\u8bf7\u7efc\u5408\u6240\u6709\u5e27\u7406\u89e3\u5b8c\u6574\u52a8\u4f5c\u3001\u8868\u60c5\u548c\u573a\u666f\u53d8\u5316\uff0c\u4e0d\u8981\u53ea\u4f9d\u636e\u7b2c\u4e00\u5f20\u56fe\u3002"
)
DEFAULT_SINGLE_HINT = (
    "[\u7cfb\u7edf\u63d0\u793a] \u672c\u6b21\u7528\u6237\u53d1\u9001\u4e86 GIF \u52a8\u56fe\uff0c\u4f46\u5f53\u524d\u4ec5\u4fdd\u7559\u4e86 1 \u5f20\u9759\u6001\u56fe\u3002"
    "\u53ef\u80fd\u4f1a\u4e22\u5931\u90e8\u5206\u52a8\u4f5c\u4fe1\u606f\uff0c\u8bf7\u7ed3\u5408\u4e0a\u4e0b\u6587\u8c28\u614e\u5224\u65ad\u3002"
)


@dataclass(frozen=True)
class SamplingPolicy:
    max_side: int
    jpeg_quality: int
    frames_le_4: int
    frames_le_8: int
    frames_le_16: int
    frames_gt_16: int
    min_frames_after_penalty: int
    threshold_mb_1: float
    threshold_mb_2: float
    penalty_1: int
    penalty_2: int


@dataclass(frozen=True)
class HintPolicy:
    enabled: bool
    target: str
    multi_frame_template: str
    single_frame_template: str


@dataclass(frozen=True)
class PluginSettings:
    enabled: bool
    cleanup_ttl_hours: int
    sampling: SamplingPolicy
    hint: HintPolicy


@register(PLUGIN_ID, "YanL", PLUGIN_DESC, PLUGIN_VERSION, PLUGIN_REPO)
class GifVisionHelper(Star):
    """Turn GIF images into frame sequences for multimodal requests."""

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | dict[str, Any] | None = None,
    ) -> None:
        super().__init__(context, config)
        self.config = config or {}

        self._temp_files: set[Path] = set()
        self._temp_files_lock = threading.Lock()
        self._temp_cache: dict[Path, float] = {}

        try:
            self._resample_lanczos = Image.Resampling.LANCZOS  # type: ignore[attr-defined]
        except Exception:
            self._resample_lanczos = getattr(Image, "LANCZOS", Image.BICUBIC)

    async def initialize(self) -> None:
        logger.info("[%s] plugin initialized", PLUGIN_ID)

    async def terminate(self) -> None:
        logger.info("[%s] cleaning up temporary sampled frames", PLUGIN_ID)
        await asyncio.to_thread(self._cleanup_temp_files)

    def _config_get(self, key: str, default: Any) -> Any:
        if hasattr(self.config, "get"):
            return self.config.get(key, default)
        return default

    def _config_section(self, key: str) -> dict[str, Any]:
        value = self._config_get(key, {})
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _read_bool(value: Any, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False
        if value is None:
            return default
        return bool(value)

    @staticmethod
    def _read_int(
        value: Any,
        default: int,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        try:
            result = int(value)
        except (TypeError, ValueError):
            result = default
        if minimum is not None:
            result = max(minimum, result)
        if maximum is not None:
            result = min(maximum, result)
        return result

    @staticmethod
    def _read_float(
        value: Any,
        default: float,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError):
            result = default
        if minimum is not None:
            result = max(minimum, result)
        if maximum is not None:
            result = min(maximum, result)
        return result

    @staticmethod
    def _read_str(value: Any, default: str) -> str:
        return value if isinstance(value, str) and value.strip() else default

    def _load_settings(self) -> PluginSettings:
        sampling_conf = self._config_section("sampling_policy")
        hint_conf = self._config_section("hint_policy")
        cleanup_conf = self._config_section("cleanup_policy")

        sampling = SamplingPolicy(
            max_side=self._read_int(
                sampling_conf.get("max_side"),
                768,
                minimum=64,
                maximum=4096,
            ),
            jpeg_quality=self._read_int(
                sampling_conf.get("jpeg_quality"),
                90,
                minimum=30,
                maximum=100,
            ),
            frames_le_4=self._read_int(
                sampling_conf.get("frames_when_total_le_4"),
                3,
                minimum=1,
                maximum=12,
            ),
            frames_le_8=self._read_int(
                sampling_conf.get("frames_when_total_le_8"),
                4,
                minimum=1,
                maximum=12,
            ),
            frames_le_16=self._read_int(
                sampling_conf.get("frames_when_total_le_16"),
                5,
                minimum=1,
                maximum=12,
            ),
            frames_gt_16=self._read_int(
                sampling_conf.get("frames_when_total_gt_16"),
                6,
                minimum=1,
                maximum=12,
            ),
            min_frames_after_penalty=self._read_int(
                sampling_conf.get("minimum_frames_after_penalty"),
                3,
                minimum=1,
                maximum=12,
            ),
            threshold_mb_1=self._read_float(
                sampling_conf.get("size_penalty_threshold_mb_1"),
                5.0,
                minimum=0.0,
                maximum=1024.0,
            ),
            threshold_mb_2=self._read_float(
                sampling_conf.get("size_penalty_threshold_mb_2"),
                10.0,
                minimum=0.0,
                maximum=1024.0,
            ),
            penalty_1=self._read_int(
                sampling_conf.get("size_penalty_step_1"),
                1,
                minimum=0,
                maximum=12,
            ),
            penalty_2=self._read_int(
                sampling_conf.get("size_penalty_step_2"),
                2,
                minimum=0,
                maximum=12,
            ),
        )

        hint_target = self._read_str(
            hint_conf.get("target"),
            "extra_user_content",
        )
        if hint_target not in {"extra_user_content", "prompt", "system_prompt"}:
            hint_target = "extra_user_content"

        hint = HintPolicy(
            enabled=self._read_bool(hint_conf.get("enabled"), True),
            target=hint_target,
            multi_frame_template=self._read_str(
                hint_conf.get("multi_frame_template"),
                DEFAULT_MULTI_HINT,
            ),
            single_frame_template=self._read_str(
                hint_conf.get("single_frame_template"),
                DEFAULT_SINGLE_HINT,
            ),
        )

        return PluginSettings(
            enabled=self._read_bool(self._config_get("enabled", True), True),
            cleanup_ttl_hours=self._read_int(
                cleanup_conf.get("cache_ttl_hours"),
                24,
                minimum=1,
                maximum=24 * 30,
            ),
            sampling=sampling,
            hint=hint,
        )

    def _register_temp_file(self, path: Path) -> None:
        now = time.time()
        with self._temp_files_lock:
            self._temp_files.add(path)
            self._temp_cache[path] = now

    def _cleanup_temp_files(self) -> None:
        with self._temp_files_lock:
            paths = list(self._temp_files)

        for path in paths:
            try:
                if path.exists():
                    path.unlink()
            except FileNotFoundError:
                pass
            except Exception as exc:
                logger.warning("[%s] failed to remove temp file %s: %s", PLUGIN_ID, path, exc)
            finally:
                with self._temp_files_lock:
                    self._temp_files.discard(path)
                    self._temp_cache.pop(path, None)

    def _cleanup_expired_cache(self, ttl_seconds: int) -> None:
        now = time.time()
        with self._temp_files_lock:
            items = list(self._temp_cache.items())

        for path, created_at in items:
            if now - created_at <= ttl_seconds:
                continue

            try:
                if path.exists():
                    path.unlink()
            except FileNotFoundError:
                pass
            except Exception as exc:
                logger.warning(
                    "[%s] failed to remove expired temp file %s: %s",
                    PLUGIN_ID,
                    path,
                    exc,
                )
            finally:
                with self._temp_files_lock:
                    self._temp_cache.pop(path, None)
                    self._temp_files.discard(path)

    @staticmethod
    def _resolve_local_image_path(image_ref: str) -> Path | None:
        if image_ref.startswith(("http://", "https://", "data:", "base64://")):
            return None

        if image_ref.startswith("file://"):
            image_ref = unquote(image_ref[7:]).lstrip("/")

        path = Path(image_ref)
        return path if path.exists() and path.is_file() else None

    def _is_gif_file(self, path: Path) -> bool:
        try:
            with path.open("rb") as file:
                header = file.read(6)
        except FileNotFoundError:
            return False
        except OSError as exc:
            logger.warning("[%s] failed to read image header %s: %s", PLUGIN_ID, path, exc)
            return False

        return header in (b"GIF87a", b"GIF89a")

    def _decide_frame_count(self, total_frames: int, file_size: int, policy: SamplingPolicy) -> int:
        if total_frames <= 1:
            return 1

        if total_frames <= 4:
            frames = min(policy.frames_le_4, total_frames)
        elif total_frames <= 8:
            frames = min(policy.frames_le_8, total_frames)
        elif total_frames <= 16:
            frames = min(policy.frames_le_16, total_frames)
        else:
            frames = min(policy.frames_gt_16, total_frames)

        first_threshold, second_threshold = sorted(
            (int(policy.threshold_mb_1 * MB), int(policy.threshold_mb_2 * MB))
        )
        first_penalty, second_penalty = (
            policy.penalty_1,
            max(policy.penalty_1, policy.penalty_2),
        )

        if file_size > second_threshold:
            frames = max(policy.min_frames_after_penalty, frames - second_penalty)
        elif file_size > first_threshold:
            frames = max(policy.min_frames_after_penalty, frames - first_penalty)

        return max(1, min(frames, total_frames))

    @staticmethod
    def _sample_indices(n_frames: int, target: int) -> list[int]:
        if n_frames <= 0:
            return []
        if target <= 1:
            return [0]
        if n_frames <= target:
            return list(range(n_frames))

        step = (n_frames - 1) / (target - 1)
        indices = sorted({int(round(i * step)) for i in range(target)})
        if not indices:
            return [0]

        indices[0] = 0
        indices[-1] = n_frames - 1
        return indices

    def _resize_frame(self, image: Image.Image, max_side: int) -> Image.Image:
        width, height = image.size
        longest_side = max(width, height)
        if longest_side <= max_side:
            return image

        scale = max_side / float(longest_side)
        new_size = (max(1, int(width * scale)), max(1, int(height * scale)))

        try:
            return image.resize(new_size, resample=self._resample_lanczos)
        except Exception as exc:
            logger.warning(
                "[%s] resize with lanczos failed, falling back to default resample: %s",
                PLUGIN_ID,
                exc,
            )
            return image.resize(new_size)

    def _convert_gif_to_multi_jpeg(
        self,
        main_path: Path,
        policy: SamplingPolicy,
    ) -> tuple[list[Path], int]:
        try:
            file_size = main_path.stat().st_size
        except FileNotFoundError:
            return [], 0

        paths: list[Path] = []

        try:
            with Image.open(main_path) as image:
                total_frames = getattr(image, "n_frames", 1)

                if total_frames <= 1:
                    first_frame = self._resize_frame(image.convert("RGB"), policy.max_side)
                    first_frame.save(main_path, format="JPEG", quality=policy.jpeg_quality)
                    return [main_path], 1

                target = self._decide_frame_count(total_frames, file_size, policy)
                indices = self._sample_indices(total_frames, target)
                if not indices:
                    return [], 0

                parent = main_path.parent
                stem = main_path.stem
                first_frame: Image.Image | None = None

                for frame_index in indices:
                    try:
                        image.seek(frame_index)
                        frame = image.convert("RGB")
                    except (EOFError, UnidentifiedImageError):
                        continue

                    frame = self._resize_frame(frame, policy.max_side)
                    if first_frame is None:
                        first_frame = frame.copy()
                        continue
                    else:
                        output_path = parent / f"{stem}_f{frame_index}.jpg"

                    frame.save(output_path, format="JPEG", quality=policy.jpeg_quality)
                    paths.append(output_path)

                    if output_path != main_path:
                        self._register_temp_file(output_path)

                if first_frame is None:
                    return [], 0

                first_frame.save(main_path, format="JPEG", quality=policy.jpeg_quality)
                paths.insert(0, main_path)
                return (paths, len(paths)) if paths else ([], 0)

        except DecompressionBombError as exc:
            logger.warning("[%s] gif rejected by Pillow bomb protection: %s", PLUGIN_ID, exc)
            return [], 0
        except Exception as exc:
            logger.error(
                "[%s] failed while converting gif %s: %s",
                PLUGIN_ID,
                main_path,
                exc,
                exc_info=True,
            )
            return [], 0

    @staticmethod
    def _format_hint(template: str, fallback: str, *, gif_count: int, frame_count: int) -> str:
        try:
            return template.format(gif_count=gif_count, frame_count=frame_count)
        except Exception:
            return fallback.format(gif_count=gif_count, frame_count=frame_count)

    def _build_hint_text(self, policy: HintPolicy, *, gif_count: int, frame_count: int) -> str:
        if frame_count > 1:
            return self._format_hint(
                policy.multi_frame_template,
                DEFAULT_MULTI_HINT,
                gif_count=gif_count,
                frame_count=frame_count,
            )
        return self._format_hint(
            policy.single_frame_template,
            DEFAULT_SINGLE_HINT,
            gif_count=gif_count,
            frame_count=frame_count,
        )

    @staticmethod
    def _has_hint_part(req: ProviderRequest, hint_text: str) -> bool:
        for part in getattr(req, "extra_user_content_parts", []):
            if isinstance(part, TextPart) and part.text == hint_text:
                return True
            if (
                isinstance(part, dict)
                and part.get("type") == "text"
                and part.get("text") == hint_text
            ):
                return True
        return False

    def _apply_hint(self, req: ProviderRequest, policy: HintPolicy, hint_text: str) -> None:
        if not hint_text:
            return

        if policy.target == "system_prompt":
            if hint_text not in req.system_prompt:
                req.system_prompt = (
                    f"{req.system_prompt}\n\n{hint_text}".strip()
                    if req.system_prompt
                    else hint_text
                )
            return

        if policy.target == "prompt":
            prompt = req.prompt or ""
            if hint_text not in prompt:
                req.prompt = f"{hint_text}\n\n{prompt}".strip()
            return

        if not self._has_hint_part(req, hint_text):
            req.extra_user_content_parts.append(TextPart(text=hint_text).mark_as_temp())

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        _ = event
        settings = self._load_settings()
        if not settings.enabled:
            return

        image_urls = getattr(req, "image_urls", None)
        if not image_urls or not isinstance(image_urls, list):
            return

        gif_candidates: list[tuple[int, Path]] = []
        for index, image_ref in enumerate(image_urls):
            if not isinstance(image_ref, str):
                continue
            local_path = self._resolve_local_image_path(image_ref)
            if local_path and self._is_gif_file(local_path):
                gif_candidates.append((index, local_path))

        if not gif_candidates:
            return

        ttl_seconds = settings.cleanup_ttl_hours * 60 * 60
        asyncio.create_task(asyncio.to_thread(self._cleanup_expired_cache, ttl_seconds))

        replacements: dict[int, list[str]] = {}
        total_sampled_frames = 0

        for index, gif_path in gif_candidates:
            logger.info("[%s] processing gif attachment: %s", PLUGIN_ID, gif_path)
            frame_paths, frame_count = await asyncio.to_thread(
                self._convert_gif_to_multi_jpeg,
                gif_path,
                settings.sampling,
            )
            if not frame_paths or frame_count <= 0:
                continue

            replacements[index] = [str(path) for path in frame_paths]
            total_sampled_frames += frame_count

        if not replacements:
            return

        new_urls: list[str] = []
        for index, image_ref in enumerate(image_urls):
            replacement = replacements.get(index)
            if replacement:
                new_urls.extend(replacement)
            else:
                new_urls.append(image_ref if isinstance(image_ref, str) else str(image_ref))

        req.image_urls = new_urls

        if settings.hint.enabled:
            hint_text = self._build_hint_text(
                settings.hint,
                gif_count=len(replacements),
                frame_count=total_sampled_frames,
            )
            self._apply_hint(req, settings.hint, hint_text)

        logger.info(
            "[%s] expanded %s GIF attachment(s): image_urls %s -> %s",
            PLUGIN_ID,
            len(replacements),
            len(image_urls),
            len(new_urls),
        )

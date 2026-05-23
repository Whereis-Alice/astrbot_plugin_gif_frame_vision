"""
AstrBot plugin: GIF Frame Vision.

Convert local GIF attachments into sampled JPEG frames before a vision model
receives them, so the model can reason about motion instead of a single still.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from PIL import Image, UnidentifiedImageError
from PIL.Image import DecompressionBombError

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image as AstrImage
from astrbot.api.message_components import Reply
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.message import TextPart
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
from astrbot.core.utils.io import download_image_by_url


PLUGIN_ID = "astrbot_plugin_gif_frame_vision"
PLUGIN_VERSION = "0.7.4"
PLUGIN_DESC = "\u5c06 QQ GIF \u52a8\u56fe\u62c6\u6210\u591a\u5e27\u9759\u6001\u56fe\uff0c\u8ba9\u591a\u6a21\u6001\u6a21\u578b\u66f4\u7a33\u5b9a\u5730\u7406\u89e3\u52a8\u6001\u5185\u5bb9"
PLUGIN_REPO = "https://github.com/Whereis-Alice/astrbot_plugin_gif_frame_vision"

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


@dataclass(frozen=True)
class GifCandidate:
    index: int
    path: Path
    label: str


@register(PLUGIN_ID, "YanL", PLUGIN_DESC, PLUGIN_VERSION, PLUGIN_REPO)
class GifFrameVision(Star):
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
    def _track_temp_file(event: AstrMessageEvent, path: Path) -> None:
        tracker = getattr(event, "track_temporary_local_file", None)
        if callable(tracker):
            tracker(str(path))

    @staticmethod
    def _make_temp_path(suffix: str) -> Path:
        temp_root = Path(get_astrbot_temp_path())
        temp_root.mkdir(parents=True, exist_ok=True)
        return temp_root / f"{PLUGIN_ID}_{uuid.uuid4().hex}{suffix}"

    @staticmethod
    def _is_under_directory(path: Path, directory: Path) -> bool:
        try:
            path.resolve().relative_to(directory.resolve())
        except ValueError:
            return False
        except OSError:
            return False
        return True

    @staticmethod
    def _resolve_local_image_path(image_ref: str) -> Path | None:
        if image_ref.startswith(("http://", "https://", "data:", "base64://")):
            return None

        if image_ref.startswith("file:///"):
            image_ref = unquote(image_ref[8:])
        elif image_ref.startswith("file://"):
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

    @staticmethod
    def _is_gif_header(header: bytes | None) -> bool:
        return header in (b"GIF87a", b"GIF89a")

    @staticmethod
    def _peek_remote_header(image_ref: str) -> bytes | None:
        request = Request(
            image_ref,
            headers={
                "Range": "bytes=0-5",
                "User-Agent": f"{PLUGIN_ID}/{PLUGIN_VERSION}",
            },
        )
        try:
            with urlopen(request, timeout=10) as response:
                return response.read(6)
        except Exception as exc:
            logger.debug("[%s] failed to peek remote image header %s: %s", PLUGIN_ID, image_ref, exc)
            return None

    def _write_temp_gif_bytes(self, image_bytes: bytes) -> Path | None:
        if not self._is_gif_header(image_bytes[:6]):
            return None

        output_path = self._make_temp_path(".gif")
        try:
            output_path.write_bytes(image_bytes)
        except OSError as exc:
            logger.warning("[%s] failed to write temp gif: %s", PLUGIN_ID, exc)
            return None

        self._register_temp_file(output_path)
        return output_path

    def _decode_base64_gif(self, image_ref: str) -> Path | None:
        payload = image_ref
        if image_ref.startswith("base64://"):
            payload = image_ref.removeprefix("base64://")
        elif image_ref.startswith("data:"):
            meta, separator, data = image_ref.partition(",")
            if not separator or ";base64" not in meta.lower():
                return None
            payload = data

        try:
            image_bytes = base64.b64decode("".join(payload.split()), validate=True)
        except (binascii.Error, ValueError) as exc:
            logger.warning("[%s] failed to decode base64 image: %s", PLUGIN_ID, exc)
            return None

        return self._write_temp_gif_bytes(image_bytes)

    async def _resolve_gif_source_path(self, image_ref: str) -> Path | None:
        local_path = self._resolve_local_image_path(image_ref)
        if local_path:
            return local_path if self._is_gif_file(local_path) else None

        if image_ref.startswith(("base64://", "data:")):
            return self._decode_base64_gif(image_ref)

        if image_ref.startswith(("http://", "https://")):
            header = await asyncio.to_thread(self._peek_remote_header, image_ref)
            if header is not None and not self._is_gif_header(header):
                return None
            if header is None and Path(urlparse(image_ref).path).suffix.lower() != ".gif":
                return None

            output_path = self._make_temp_path(".gif")
            try:
                downloaded = Path(await download_image_by_url(image_ref, path=str(output_path)))
            except Exception as exc:
                logger.warning("[%s] failed to download gif candidate %s: %s", PLUGIN_ID, image_ref, exc)
                return None

            if not self._is_gif_file(downloaded):
                try:
                    downloaded.unlink(missing_ok=True)
                except Exception:
                    pass
                return None

            self._register_temp_file(downloaded)
            return downloaded

        return None

    @staticmethod
    def _read_content_part_text(part: Any) -> str | None:
        if isinstance(part, TextPart):
            return part.text
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text")
            return text if isinstance(text, str) else None
        return None

    @staticmethod
    def _parse_image_attachment_marker(text: str) -> tuple[str, bool] | None:
        prefixes = (
            ("[Image Attachment: path ", False),
            ("[Image Attachment in quoted message: path ", True),
        )
        for prefix, quoted in prefixes:
            if text.startswith(prefix) and text.endswith("]"):
                return text[len(prefix) : -1].strip(), quoted
        return None

    def _collect_image_marker_gif_candidates(
        self,
        req: ProviderRequest,
        image_count: int,
    ) -> dict[int, GifCandidate]:
        candidates: dict[int, GifCandidate] = {}
        marker_index = 0
        for part in getattr(req, "extra_user_content_parts", []):
            text = self._read_content_part_text(part)
            if not text:
                continue
            parsed = self._parse_image_attachment_marker(text)
            if not parsed:
                continue

            path_ref, _ = parsed
            local_path = self._resolve_local_image_path(path_ref)
            if local_path and self._is_gif_file(local_path) and marker_index < image_count:
                candidates[marker_index] = GifCandidate(
                    index=marker_index,
                    path=local_path,
                    label=f"attachment marker image {marker_index + 1}",
                )
            marker_index += 1
        return candidates

    async def _collect_request_gif_candidates(
        self,
        image_urls: list[Any],
    ) -> dict[int, GifCandidate]:
        candidates: dict[int, GifCandidate] = {}
        for index, image_ref in enumerate(image_urls):
            if not isinstance(image_ref, str):
                continue
            path = await self._resolve_gif_source_path(image_ref)
            if path and self._is_gif_file(path):
                candidates[index] = GifCandidate(
                    index=index,
                    path=path,
                    label=f"request image {index + 1}",
                )
        return candidates

    async def _resolve_image_component_path(self, component: AstrImage) -> Path | None:
        saw_source_ref = False
        for attr in ("path", "file", "url"):
            value = getattr(component, attr, None)
            if not isinstance(value, str) or not value:
                continue
            saw_source_ref = True
            path = await self._resolve_gif_source_path(value)
            if path:
                return path

        if saw_source_ref:
            return None

        converter = getattr(component, "convert_to_file_path", None)
        if callable(converter):
            try:
                resolved = await converter()
            except Exception as exc:
                logger.warning("[%s] failed to resolve image component: %s", PLUGIN_ID, exc)
                return None
            local_path = self._resolve_local_image_path(str(resolved))
            if local_path and self._is_gif_file(local_path):
                if self._is_under_directory(local_path, Path(get_astrbot_temp_path())):
                    self._register_temp_file(local_path)
                return local_path

        return None

    async def _collect_event_gif_candidates(
        self,
        event: AstrMessageEvent,
        image_count: int,
    ) -> dict[int, GifCandidate]:
        candidates: dict[int, GifCandidate] = {}
        direct_slots: list[GifCandidate | None] = []
        quoted_slots: list[GifCandidate | None] = []

        for comp in event.get_messages():
            if isinstance(comp, AstrImage):
                path = await self._resolve_image_component_path(comp)
                direct_index = len(direct_slots)
                if path and self._is_gif_file(path):
                    direct_slots.append(
                        GifCandidate(
                            index=direct_index,
                            path=path,
                            label=f"event image {direct_index + 1}",
                        ),
                    )
                else:
                    direct_slots.append(None)
                continue

            if isinstance(comp, Reply) and comp.chain:
                for reply_comp in comp.chain:
                    if not isinstance(reply_comp, AstrImage):
                        continue
                    path = await self._resolve_image_component_path(reply_comp)
                    quoted_index = len(quoted_slots)
                    if path and self._is_gif_file(path):
                        quoted_slots.append(
                            GifCandidate(
                                index=quoted_index,
                                path=path,
                                label=f"quoted event image {quoted_index + 1}",
                            ),
                        )
                    else:
                        quoted_slots.append(None)

        for index, candidate in enumerate(direct_slots + quoted_slots):
            if candidate is None or index >= image_count:
                continue
            candidates[index] = GifCandidate(
                index=index,
                path=candidate.path,
                label=candidate.label,
            )

        return candidates

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
                    output_path = self._make_temp_path("_f0.jpg")
                    first_frame.save(output_path, format="JPEG", quality=policy.jpeg_quality)
                    self._register_temp_file(output_path)
                    return [output_path], 1

                target = self._decide_frame_count(total_frames, file_size, policy)
                indices = self._sample_indices(total_frames, target)
                if not indices:
                    return [], 0

                for frame_index in indices:
                    try:
                        image.seek(frame_index)
                        frame = image.convert("RGB")
                    except (EOFError, UnidentifiedImageError):
                        continue

                    frame = self._resize_frame(frame, policy.max_side)
                    output_path = self._make_temp_path(f"_f{frame_index}.jpg")
                    frame.save(output_path, format="JPEG", quality=policy.jpeg_quality)
                    paths.append(output_path)
                    self._register_temp_file(output_path)

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
        settings = self._load_settings()
        if not settings.enabled:
            return

        image_urls = getattr(req, "image_urls", None)
        if not image_urls or not isinstance(image_urls, list):
            return

        request_candidates = await self._collect_request_gif_candidates(image_urls)
        marker_candidates = self._collect_image_marker_gif_candidates(req, len(image_urls))
        event_candidates = await self._collect_event_gif_candidates(event, len(image_urls))

        gif_candidates = {
            **event_candidates,
            **marker_candidates,
            **request_candidates,
        }

        if not gif_candidates:
            return

        ttl_seconds = settings.cleanup_ttl_hours * 60 * 60
        asyncio.create_task(asyncio.to_thread(self._cleanup_expired_cache, ttl_seconds))

        replacements: dict[int, list[str]] = {}
        total_sampled_frames = 0

        for index, candidate in sorted(gif_candidates.items()):
            logger.info(
                "[%s] processing gif attachment from %s: %s",
                PLUGIN_ID,
                candidate.label,
                candidate.path,
            )
            frame_paths, frame_count = await asyncio.to_thread(
                self._convert_gif_to_multi_jpeg,
                candidate.path,
                settings.sampling,
            )
            if not frame_paths or frame_count <= 0:
                continue

            for path in frame_paths:
                self._track_temp_file(event, path)
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

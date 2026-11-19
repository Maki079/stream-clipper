"""config.yaml schema, loading and validation.

Everything the app does is driven by this file. Validation happens once at startup;
errors are printed as a readable list (path -> problem) instead of a stack trace.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

PLACEHOLDER = "FILL_ME"


class Strict(BaseModel):
    """Base model: unknown keys are errors (catches typos in config.yaml)."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- global ----


class BufferCfg(Strict):
    segment_seconds: int = Field(5, ge=1, le=30)
    retention_seconds: int = Field(300, ge=60, le=3600)
    poll_interval_seconds: int = Field(60, ge=10, description="how often to check if an offline channel went live")


class WorkersCfg(Strict):
    cpu_workers: int = Field(3, ge=1, le=32, description="transcribe/render/analyze processes")
    threads_per_worker: int = Field(4, ge=1, le=32)
    io_slots: int = Field(4, ge=1, le=32, description="concurrent download/cut/upload/notify jobs")
    max_attempts: int = Field(5, ge=1, le=50)
    retry_base_seconds: int = Field(30, ge=1)
    stale_job_seconds: int = Field(900, ge=60, description="running job without heartbeat this long is requeued")


class WhisperCfg(Strict):
    model: str = "medium"
    vod_scoring_model: str = Field("small", description="model for full-length VOD transcription (scoring only)")
    compute_type: str = "int8"
    device: Literal["cpu"] = "cpu"
    vad: bool = True
    beam_size: int = Field(5, ge=1, le=10)


class MotionCfg(Strict):
    """Smart zoom + motion (see stream_clipper/media/motion.py)."""
    enabled: bool = True
    smart_zoom: bool = Field(True, description="punch-in zooms on loud reactions and the chat-spike moment")
    zoom_strength: float = Field(0.15, ge=0, le=0.6, description="0.15 = zoom to 115 %")
    max_zooms_per_minute: float = Field(3, ge=0, le=12)
    min_gap_seconds: float = Field(7, ge=1)
    hold_seconds: float = Field(1.6, ge=0.2, le=10)
    punch_in_seconds: float = Field(0.2, ge=0.05, le=3)
    punch_out_seconds: float = Field(0.6, ge=0.05, le=5)
    audio_z: float = Field(2.5, gt=0, description="how much louder than usual a moment must be to zoom")
    ken_burns: float = Field(0.05, ge=0, le=0.3, description="slow push-in over the whole clip (0.05 = to 105 %)")
    shake: bool = Field(True, description="short camera shake on the strongest moment")
    shake_px: float = Field(9, ge=0, le=40)
    shake_seconds: float = Field(0.45, gt=0, le=3)
    shake_hz: float = Field(9, gt=0, le=30)


class OutputCfg(Strict):
    width: int = 1080
    height: int = 1920
    fps: int = Field(30, ge=24, le=60)
    video_bitrate: str = "8M"
    maxrate: str = "10M"
    bufsize: str = "16M"
    preset: str = "medium"
    audio_bitrate: str = "160k"
    font: str = "Montserrat ExtraBold"
    font_size: int = Field(80, ge=30, le=160)
    words_per_chunk_min: int = Field(3, ge=1)
    words_per_chunk_max: int = Field(5, ge=1)
    max_chars_per_line: int = Field(18, ge=8)
    subtitle_uppercase: bool = True
    highlight_active_word: bool = True
    highlight_color: str = Field("#FFE600", pattern=r"^#[0-9A-Fa-f]{6}$")
    fonts_dir: str = Field("/app/assets/fonts", description="extra fonts for subtitles (Montserrat is shipped)")
    motion: MotionCfg = MotionCfg()

    @model_validator(mode="after")
    def _chunks(self) -> OutputCfg:
        if self.words_per_chunk_min > self.words_per_chunk_max:
            raise ValueError("words_per_chunk_min must be <= words_per_chunk_max")
        return self


class RcloneCfg(Strict):
    remote: str = "gdrive"
    root: str = "stream-clipper"
    config_path: str = "/config/rclone/rclone.conf"
    extra_args: list[str] = []


class DeliveryCfg(Strict):
    backend: Literal["local", "rclone"] = "local"
    local_dir: str = "/output"
    rclone: RcloneCfg = RcloneCfg()
    delete_work_files: bool = True


class TelegramCfg(Strict):
    bot_token: str = ""
    chat_id: str = Field("", description="empty = auto: the first chat that sends /start to the bot")
    send_video: bool = Field(True, description="send every clip as a video + copy-ready captions")
    max_upload_mb: int = Field(49, ge=5, le=2000, description="Bot API limit is 50 MB; bigger clips get a smaller copy")


class NtfyCfg(Strict):
    server: str = "https://ntfy.sh"
    topic: str = ""
    token: str = ""


class NotificationsCfg(Strict):
    backend: Literal["none", "telegram", "ntfy"] = "none"
    telegram: TelegramCfg = TelegramCfg()
    ntfy: NtfyCfg = NtfyCfg()
    top_n: int = Field(3, ge=1, le=20, description="clips per 'new clips' message")
    batch_seconds: int = Field(300, ge=0, description="collect finished clips this long before notifying")
    daily_summary_time: str = "20:00"
    spike_alerts: bool = Field(False, description="short silent message as soon as a chat spike starts a clip")

    @field_validator("daily_summary_time")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("must be HH:MM (24h)")
        return v

    @model_validator(mode="after")
    def _creds(self) -> NotificationsCfg:
        if self.backend == "telegram" and not self.telegram.bot_token:
            raise ValueError("telegram backend needs telegram.bot_token (from @BotFather)")
        if self.backend == "ntfy" and not self.ntfy.topic:
            raise ValueError("ntfy backend needs ntfy.topic")
        return self


class LlmCfg(Strict):
    enabled: bool = False
    ollama_url: str = "http://ollama:11434"
    model: str = "qwen3.5:4b"
    think: bool = Field(False, description="reasoning mode of thinking models (qwen3.x); slow on CPU")
    timeout_seconds: int = Field(300, ge=10)
    top_k_candidates: int = Field(15, ge=1, description="only this many best heuristic windows are sent to the LLM")
    weight: float = Field(1.0, ge=0)
    # live clips: the LLM reads transcript + chat before rendering (README section 3c)
    live_judge: bool = Field(True, description="rate live clips; below min_score they are not rendered")
    min_score: float = Field(5.0, ge=0, le=10)
    keep_strength: float = Field(28.0, ge=0, description="chat spikes this strong are never skipped (often visual)")
    trim: bool = Field(True, description="let the LLM choose start/end (setup -> payoff) inside extra context")
    context_seconds: float = Field(20.0, ge=0, le=60, description="extra footage cut before/after for trimming")
    hook_overlay: bool = Field(True, description="burn the hook title on top of the first seconds")
    hook_seconds: float = Field(3.0, ge=0, le=10)


class GlobalCfg(Strict):
    data_dir: str = "/data"
    buffer_dir: str = "/buffer"
    models_dir: str = "/models"
    timezone: str = "Europe/Bratislava"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    buffer: BufferCfg = BufferCfg()
    workers: WorkersCfg = WorkersCfg()
    whisper: WhisperCfg = WhisperCfg()
    output: OutputCfg = OutputCfg()
    delivery: DeliveryCfg = DeliveryCfg()
    notifications: NotificationsCfg = NotificationsCfg()
    llm: LlmCfg = LlmCfg()


# ------------------------------------------------------------- campaigns ----

Platform = Literal["tiktok", "instagram", "youtube"]


class CampaignCfg(Strict):
    required_hashtags: list[str] = []
    extra_hashtags: list[str] = []
    creator_name: str = Field(..., description="text that must appear in the caption, e.g. @kaicenat")
    creator_aliases: list[str] = Field([], description="words that identify this creator in transcript/chat")
    caption_language: str = Field("en", description="Fallback caption language when the source language and detected speech language are unavailable; captions otherwise follow the streamer")
    allowed_platforms: list[Platform] = ["tiktok", "instagram", "youtube"]
    min_seconds: int = Field(61, ge=5)
    max_seconds: int = Field(90, ge=5)
    max_caption_chars: int = Field(2200, ge=50)
    hook_templates: list[str] = []
    notes: str = ""

    @field_validator("required_hashtags", "extra_hashtags")
    @classmethod
    def _hashtags(cls, v: list[str]) -> list[str]:
        out = []
        for tag in v:
            tag = tag.strip()
            if not tag.startswith("#"):
                tag = "#" + tag
            if " " in tag or len(tag) < 2:
                raise ValueError(f"invalid hashtag {tag!r}")
            out.append(tag)
        return out

    @model_validator(mode="after")
    def _lengths(self) -> CampaignCfg:
        if self.min_seconds > self.max_seconds:
            raise ValueError(f"min_seconds ({self.min_seconds}) > max_seconds ({self.max_seconds})")
        return self


# --------------------------------------------------------------- sources ----


class Rect(Strict):
    x: int = Field(..., ge=0)
    y: int = Field(..., ge=0)
    w: int = Field(..., gt=0)
    h: int = Field(..., gt=0)

    def ffmpeg(self) -> str:
        return f"crop={self.w}:{self.h}:{self.x}:{self.y}"


class LayoutCfg(Strict):
    type: Literal["stack", "center_crop", "blur_fit"] = "blur_fit"
    facecam: Rect | None = None
    gameplay: Rect | None = None
    main: Rect | None = Field(None, description="center_crop: region of the main subject (default: centered 9:16)")
    facecam_height: int = Field(700, ge=200, le=1400)
    subtitle_y: float | None = Field(None, gt=0, lt=1, description="override: subtitle center as fraction of height")

    @model_validator(mode="after")
    def _needs(self) -> LayoutCfg:
        if self.type == "stack" and (self.facecam is None or self.gameplay is None):
            raise ValueError("layout 'stack' needs both facecam and gameplay crop rectangles")
        return self


class SpikeCfg(Strict):
    window_seconds: int = Field(10, ge=2, le=60)
    baseline_seconds: int = Field(300, ge=30)
    # threshold = max(min_messages, multiplier * avg, avg + z_threshold * std), avg/std over baseline_seconds
    multiplier: float = Field(2.0, gt=1, description="window must have at least this many x the rolling average")
    z_threshold: float = Field(3.0, gt=0, description="...and be this many std above it")
    min_messages: int = Field(15, ge=1, description="absolute floor, only matters for quiet chats")
    min_unique_chatters: int = Field(0, ge=0)
    metric: Literal["messages", "unique", "either", "both"] = "either"
    warmup_seconds: int = Field(180, ge=0)
    cooldown_seconds: int = Field(90, ge=0)
    min_strength: float = Field(14.0, ge=0, description="spikes weaker than this don't start a clip (see Spike.strength)")
    max_clips_per_hour: int = Field(4, ge=0, description="0 = unlimited; over budget only spikes >= 2x min_strength pass")
    ignore_patterns: list[str] = [
        r"^!\w+",                                     # bot commands
        r"(?i)hype train",
        r"^[\d\s\-:/.,]+$",                           # numbers only: polls, scores, "67"
        r"(?i)^(yes|no|y|n|rt|stare|first|1st|hi|hello|hey)\W*$",  # poll answers / greetings / filler
        r"(?i)\b(vape|bong|goon|fent|piss|pee|bathroom|smoke|food)\s*break\b",  # streamer is AFK
    ]
    ignore_users: list[str] = ["nightbot", "streamelements", "streamlabs", "moobot", "fossabot", "wizebot", "sery_bot"]

    @field_validator("ignore_patterns")
    @classmethod
    def _regex(cls, v: list[str]) -> list[str]:
        for p in v:
            try:
                re.compile(p)
            except re.error as e:
                raise ValueError(f"bad regex {p!r}: {e}") from e
        return v


class ClipCfg(Strict):
    pre_seconds: int = Field(30, ge=0, le=300)
    post_seconds: int = Field(35, ge=0, le=300)
    max_seconds: int = Field(120, ge=10, le=600)
    chat_delay_seconds: float = Field(6.0, ge=0, le=60, description="chat reaction lag, subtracted from spike time")


class ScoringWeights(Strict):
    audio_energy: float = 1.0
    shouting: float = 1.0
    laughter: float = 0.4
    speech_density: float = 0.6
    excitement: float = 0.8
    scene_density: float = 0.4


class VodCfg(Strict):
    watch: bool = Field(False, description="poll url (channel/playlist/folder) for new videos")
    watch_interval_seconds: int = Field(1800, ge=60)
    watch_max_items: int = Field(5, ge=1, description="newest N items considered per poll")
    top_n: int = Field(10, ge=1, le=100)
    min_seconds: int | None = Field(None, description="override; default = strictest campaign range")
    max_seconds: int | None = None
    keywords: list[str] = ["no way", "oh my god", "what", "bro", "let's go", "insane", "crazy", "wait"]
    weights: ScoringWeights = ScoringWeights()
    scene_threshold: float = Field(0.3, gt=0, lt=1)
    keep_download: bool = False


Mode = Literal["live", "vod"]
SourcePlatform = Literal["twitch", "youtube", "kick", "generic", "gdrive", "local"]


class SourceCfg(Strict):
    enabled: bool = True
    mode: Mode
    platform: SourcePlatform | Literal["FILL_ME"]
    url: str
    language: str = "auto"
    layout: LayoutCfg = LayoutCfg()
    campaigns: list[str] = []
    clip: ClipCfg = ClipCfg()
    spike: SpikeCfg = SpikeCfg()
    vod: VodCfg = VodCfg()
    chat_backend: Literal["auto", "twitch_irc", "youtube_innertube", "youtube_chat_downloader", "youtube_api", "kick_pusher", "none"] = "auto"
    youtube_api_key: str = ""
    stream_quality: str = "best"

    @model_validator(mode="after")
    def _platform(self) -> SourceCfg:
        if self.platform == PLACEHOLDER and self.enabled:
            raise ValueError("platform is FILL_ME: set twitch/youtube/kick (or set enabled: false)")
        if self.mode == "live" and self.platform in ("gdrive", "local", "generic"):
            raise ValueError(f"live mode supports twitch/youtube/kick, not {self.platform}")
        if self.chat_backend == "youtube_api" and not self.youtube_api_key:
            raise ValueError("chat_backend youtube_api needs youtube_api_key")
        return self

    def resolved_chat_backend(self) -> str:
        if self.chat_backend != "auto":
            return self.chat_backend
        return {"twitch": "twitch_irc", "youtube": "youtube_innertube", "kick": "kick_pusher"}.get(self.platform, "none")


# ------------------------------------------------------------------ root ----


class AppConfig(Strict):
    global_: GlobalCfg = Field(GlobalCfg(), alias="global")
    campaigns: dict[str, CampaignCfg] = {}
    sources: dict[str, SourceCfg] = {}

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @field_validator("sources", "campaigns")
    @classmethod
    def _names(cls, v: dict) -> dict:
        for name in v:
            if not re.fullmatch(r"[A-Za-z0-9_\-]+", name):
                raise ValueError(f"name {name!r}: use letters, digits, '_' or '-' only")
        return v

    @model_validator(mode="after")
    def _refs(self) -> AppConfig:
        for sname, src in self.sources.items():
            for c in src.campaigns:
                if c not in self.campaigns:
                    raise ValueError(f"source {sname!r} references unknown campaign {c!r} (known: {sorted(self.campaigns)})")
        return self

    # convenience
    @property
    def g(self) -> GlobalCfg:
        return self.global_

    def enabled_sources(self, mode: Mode | None = None) -> dict[str, SourceCfg]:
        return {n: s for n, s in self.sources.items() if s.enabled and (mode is None or s.mode == mode)}

    def warnings(self) -> list[str]:
        """Non-fatal problems worth shouting about at startup."""
        out = []
        for cname, c in self.campaigns.items():
            if any(PLACEHOLDER in t for t in c.required_hashtags) or PLACEHOLDER in c.creator_name:
                used = [s for s, src in self.enabled_sources().items() if cname in src.campaigns]
                if used:
                    out.append(f"campaign {cname!r} still has {PLACEHOLDER} placeholders and is used by {used}; its captions will be flagged 'DO NOT POST AS IS'")
        for sname, s in self.enabled_sources().items():
            if not s.campaigns:
                out.append(f"source {sname!r} has no campaigns; its clips will be rendered but not delivered")
        if self.g.workers.cpu_workers * self.g.workers.threads_per_worker > 64:
            out.append("cpu_workers * threads_per_worker > 64: likely oversubscribed")
        return out


class ConfigError(Exception):
    pass


def _format_errors(err: ValidationError) -> str:
    lines = []
    for e in err.errors():
        loc = ".".join(str(p) for p in e["loc"]) or "(root)"
        msg = e["msg"].removeprefix("Value error, ")
        lines.append(f"  - {loc}: {msg}")
    return "config.yaml is invalid:\n" + "\n".join(lines)


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"config.yaml is not valid YAML: {e}") from e
    return parse_config(raw)


def parse_config(raw: dict) -> AppConfig:
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml must be a mapping with global/campaigns/sources")
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(_format_errors(e)) from None

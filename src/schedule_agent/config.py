# -*- coding: utf-8 -*-
"""集中配置管理。

加载优先级：**默认值 < 配置文件 < 环境变量**（环境变量最优先）。
`Settings` 里的**每一个字段**都在 `_ENV_MAP` 中有对应的 `SCHEDULE_*` 环境变量，
因此容器 / 虚拟机部署可以完全不落配置文件，只用环境变量。

第三方渠道（企业微信 / 钉钉 / 飞书 …）的配置**不在这里**：它们各自放在
`schedule_agent/channels/<name>/config.py` 里，由 `channels` 包自行解析
（配置文件写 `channels.<name>`，环境变量用 `SCHEDULE_CHANNEL_<NAME>_*`）。
这样新增一个平台不必再改这个文件。

- 配置文件路径：环境变量 `SCHEDULE_CONFIG`，否则依次查找
  `./config/config.json`、`./config.json`、`/etc/schedule-agent/config.json`。
- 数据目录：`SCHEDULE_DATA_DIR`（默认 `./data`），数据库与日志都放在它下面。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path

# 配置文件搜索顺序（第一个存在的生效）
_CONFIG_CANDIDATES = (
    "config/config.json",
    "config.json",
    "/etc/schedule-agent/config.json",
)

# 环境变量名 -> 配置字段名（环境变量优先级最高）
_ENV_MAP = {
    "SCHEDULE_CONFIG": None,           # 特殊：仅用于指定配置文件位置
    "SCHEDULE_DATA_DIR": "data_dir",
    "SCHEDULE_DB_PATH": "db_path",
    "SCHEDULE_AUTH_DB_PATH": "auth_db_path",
    "SCHEDULE_LOG_DIR": "log_dir",
    "SCHEDULE_LLM_API_KEY": "llm_api_key",
    "SCHEDULE_LLM_BASE_URL": "llm_base_url",
    "SCHEDULE_LLM_MODEL": "llm_model",
    "SCHEDULE_LLM_MAX_RETRIES": "llm_max_retries",
    "SCHEDULE_LLM_RETRY_DELAY": "llm_retry_delay",
    "SCHEDULE_LLM_RETRY_MAX_DELAY": "llm_retry_max_delay",
    "SCHEDULE_DAILY_SUMMARY_TIME": "daily_summary_time",
    "SCHEDULE_LEAD_MINUTES": "lead_minutes",
    "SCHEDULE_INTERVAL": "interval",
    "SCHEDULE_ADMIN_API_KEY": "admin_api_key",
    "SCHEDULE_API_HOST": "api_host",
    "SCHEDULE_API_PORT": "api_port",
    "SCHEDULE_UI_PORT": "ui_port",
    "SCHEDULE_API_URL": "api_url",
    "SCHEDULE_LOG_LEVEL": "log_level",
    "SCHEDULE_USE_RULE_FALLBACK": "use_rule_fallback",
    "SCHEDULE_LANGUAGE": "language",
    "SCHEDULE_ALLOWED_ORIGINS": "allowed_origins",
}

_INT_FIELDS = {"lead_minutes", "interval", "api_port", "ui_port",
               "llm_max_retries", "llm_retry_delay", "llm_retry_max_delay"}
_BOOL_FIELDS = {"use_rule_fallback"}
_LIST_FIELDS = {"allowed_origins"}
_TRUE_WORDS = {"1", "true", "yes", "on", "y", "t"}
_FALSE_WORDS = {"0", "false", "no", "off", "n", "f"}


@dataclass
class Settings:
    """运行期配置。路径类字段为空时由 data_dir 推导。"""

    # ---- 路径 ----
    data_dir: str = "data"          # 数据根目录（DB、日志）
    db_path: str = ""               # 默认 <data_dir>/schedule.db
    auth_db_path: str = ""          # 默认 <data_dir>/auth.db
    log_dir: str = ""               # 默认 <data_dir>/logs

    # ---- 大模型 ----
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"

    # 大模型不可用时，是否退化为「关键词规则」兜底。
    # 默认 False：意图理解只认大模型，模型挂了就如实报错，绝不用规则猜意图——
    # 规则是**闭集**，猜错时不会报错，只会悄悄建出垃圾待办（历史 bug 的根源）。
    use_rule_fallback: bool = False

    # ---- 大模型重试配置 ----
    llm_max_retries: int = 3           # 最大重试次数
    llm_retry_delay: int = 1           # 初始重试延迟（秒）
    llm_retry_max_delay: int = 10      # 最大重试延迟（秒）

    # ---- 提醒引擎 ----
    daily_summary_time: str = "08:00"
    lead_minutes: int = 10
    interval: int = 20

    # ---- 第三方接入渠道 ----
    # {渠道名: 该渠道的原始配置 dict}，由 channels 包自行解析与校验。
    # 不在这里为每个平台加字段：新增平台只改 channels/<name>/config.py。
    channels: dict = field(default_factory=dict)

    # ---- 服务 ----
    admin_api_key: str = ""         # 为空则关闭管理员能力
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    ui_port: int = 7860
    api_url: str = ""               # 前端连接后端地址，默认 http://127.0.0.1:<api_port>
    log_level: str = "INFO"
    allowed_origins: list = field(default_factory=lambda: ["*"])  # CORS 允许的源列表

    # ---- 国际化 ----
    # 默认界面 / 回复语言；可取 zh-CN / en-US（其他语言放进 i18n/locales 即可）。
    # 单个请求仍可通过 Accept-Language 或 ?lang= 覆盖，这里只是「兜底默认值」。
    language: str = "zh-CN"

    # ------------------------------------------------------------------
    def __post_init__(self):
        data = Path(self.data_dir).expanduser()
        if not self.db_path:
            self.db_path = str(data / "schedule.db")
        if not self.auth_db_path:
            self.auth_db_path = str(data / "auth.db")
        if not self.log_dir:
            self.log_dir = str(data / "logs")
        if not self.api_url:
            self.api_url = f"http://127.0.0.1:{self.api_port}"

    def ensure_dirs(self):
        """确保数据 / 日志目录存在（VM 首次启动时自动创建）。"""
        for p in (Path(self.data_dir).expanduser(),
                  Path(self.db_path).expanduser().parent,
                  Path(self.auth_db_path).expanduser().parent,
                  Path(self.log_dir).expanduser()):
            p.mkdir(parents=True, exist_ok=True)
        return self

    def channel_config(self, name: str) -> dict:
        """取某个第三方渠道的原始配置（没有就返回空 dict）。"""
        data = (self.channels or {}).get(name)
        return dict(data) if isinstance(data, dict) else {}

    def as_dict(self):
        return {f.name: getattr(self, f.name) for f in fields(self)}


def find_config_file() -> Path | None:
    """定位配置文件；显式指定的路径即使不存在也返回（便于报错提示）。"""
    env = os.environ.get("SCHEDULE_CONFIG")
    if env:
        return Path(env).expanduser()
    for rel in _CONFIG_CANDIDATES:
        p = Path(rel)
        if p.exists():
            return p
    return None


def _load_file(path: Path | None) -> dict:
    if path is None:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: 配置损坏不应导致进程崩溃，退回默认
        print(f"[config] 读取配置文件失败（{path}）：{e}，将使用默认配置。")
        return {}


def _apply_env(cfg: dict) -> dict:
    for env_name, field_name in _ENV_MAP.items():
        if field_name is None:
            continue
        val = os.environ.get(env_name)
        if val is None or val == "":
            continue
        if field_name in _INT_FIELDS:
            try:
                val = int(val)
            except ValueError:
                print(f"[config] {env_name} 不是合法整数，忽略：{val}")
                continue
        elif field_name in _BOOL_FIELDS:
            low = str(val).strip().lower()
            if low in _TRUE_WORDS:
                val = True
            elif low in _FALSE_WORDS:
                val = False
            else:
                print(f"[config] {env_name} 不是合法布尔值（1/0/true/false），忽略：{val}")
                continue
        elif field_name in _LIST_FIELDS:
            val = [v.strip() for v in val.split(",") if v.strip()]
        cfg[field_name] = val
    return cfg


def _load_dotenv() -> None:
    """把 `.env` 读进进程环境变量（**绝不覆盖已存在的真实环境变量**）。

    优先级从高到低：真实环境变量 > `.env` > 配置文件 > 内置默认。
    为什么要有这一层：很多人改了项目根的 `.env` 却以为生效了——如果不加载，
    配置会静默走回配置文件或默认值，排查起来非常费劲。纯标准库实现，无额外依赖。

    - `SCHEDULE_ENV_FILE` 可指定别的路径；
    - `SCHEDULE_NO_DOTENV=1` 可完全关闭（测试用）。
    """
    if os.environ.get("SCHEDULE_NO_DOTENV"):
        return
    path = Path(os.environ.get("SCHEDULE_ENV_FILE", ".env")).expanduser()
    if not path.is_file():
        return
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:      # 真实环境变量优先
                os.environ[key] = val
    except Exception as e:  # noqa: .env 有问题也不该阻止启动
        print(f"[config] 读取 {path} 失败（已忽略）：{e}")


def load_settings(config_file: Path | None = None) -> Settings:
    """按 默认 <- 配置文件 <- 环境变量 的顺序构建 Settings。"""
    _load_dotenv()          # 让项目根目录的 .env 也参与「环境变量」这一层
    path = config_file if config_file is not None else find_config_file()
    raw = _load_file(path)

    # 第三方渠道配置：交给 channels 包解析（新增平台不用改这里）
    from schedule_agent.channels.config import collect as _collect_channels
    channels = _collect_channels(raw)

    # 仅保留 Settings 认识的键，避免多余字段导致 TypeError
    known = {f.name for f in fields(Settings)} - {"channels"}
    clean = {k: v for k, v in raw.items() if k in known}
    clean = _apply_env(clean)
    clean["channels"] = channels
    return Settings(**clean)


_SETTINGS: Settings | None = None


def get_settings(refresh: bool = False) -> Settings:
    """全局单例式获取配置（测试可用 refresh=True 重载）。"""
    global _SETTINGS
    if _SETTINGS is None or refresh:
        _SETTINGS = load_settings()
    return _SETTINGS


def set_settings(settings: Settings) -> Settings:
    """显式注入配置（测试 / 嵌入式调用用），覆盖自动加载结果。"""
    global _SETTINGS
    _SETTINGS = settings
    return settings


def config_source() -> str:
    """返回当前生效的配置文件路径（便于启动日志里打印）。"""
    p = find_config_file()
    return str(p) if p else "（未找到配置文件，使用内置默认值）"

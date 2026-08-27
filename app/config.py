"""Settings. No I/O at import time.

The sibling project binds its MCP server map at module import and connects to Milvus from a
module-level singleton, which means importing the agent package opens sockets — and makes
the CLI, the tests and the Slack process all pay for a dependency they may not use. Nothing
here connects to anything; construction is the caller's job.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "oncall-agent"
    app_version: str = "2.0.0"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 9900

    # --- model ---------------------------------------------------------------
    llm_provider: str = "dashscope"  # dashscope | openai
    dashscope_api_key: str = ""
    dashscope_api_base: str = ""
    openai_api_key: str = ""
    openai_api_base: str = ""
    model_deep: str = "qwen-max"
    model_fast: str = "qwen-plus"
    llm_max_attempts: int = 4

    # --- retrieval -----------------------------------------------------------
    milvus_host: str = "localhost"
    milvus_port: int = 19530
    milvus_collection: str = "oncall_knowledge"
    dashscope_embedding_model: str = "text-embedding-v4"
    rag_top_k: int = 3
    chunk_max_size: int = 800
    chunk_overlap: int = 100

    # --- metrics / logs ------------------------------------------------------
    grafana_url: str = ""
    grafana_token: str = ""
    prometheus_base_url: str = "http://127.0.0.1:9090"
    prometheus_request_timeout: float = 10.0

    # --- MCP -----------------------------------------------------------------
    mcp_grafana_url: str = "http://localhost:8005/mcp"
    mcp_monitor_url: str = "http://localhost:8004/mcp"
    mcp_cls_url: str = "http://localhost:8003/mcp"
    mcp_transport: str = "streamable-http"
    mcp_enabled: str = "grafana"  # comma-separated subset of the keys below

    # --- repos ---------------------------------------------------------------
    repo_root: Path = Path.home() / "Project"
    knowledge_repo: Path = Path.home() / "Project" / "rec-knowledge"

    # --- budgets (graph guards; the model has no vote) -----------------------
    max_steps: int = 8
    replan_ban_after: int = 5
    wall_clock_seconds: float = 120.0
    query_window_minutes: int = 60

    # --- persistence ---------------------------------------------------------
    database_url: str = ""

    # --- slack ---------------------------------------------------------------
    slack_bot_token: str = ""
    slack_app_token: str = ""
    slack_alert_bot_ids: list[str] = Field(default_factory=list)
    slack_alert_app_ids: list[str] = Field(default_factory=list)
    slack_alert_channels: list[str] = Field(default_factory=list)
    slack_use_streaming: bool = False
    slack_progress_interval: float = 1.5
    slack_thread_limit: int = 50

    @property
    def mcp_servers(self) -> dict[str, dict[str, Any]]:
        """Only the servers named in `mcp_enabled`. A URL that is configured but not
        enabled is not dialled — an unreachable MCP server should be a choice, not a
        surprise at first tool load."""
        available = {
            "grafana": self.mcp_grafana_url,
            "monitor": self.mcp_monitor_url,
            "cls": self.mcp_cls_url,
        }
        enabled = {n.strip() for n in self.mcp_enabled.split(",") if n.strip()}
        return {
            name: {"transport": self.mcp_transport, "url": url}
            for name, url in available.items()
            if name in enabled
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()

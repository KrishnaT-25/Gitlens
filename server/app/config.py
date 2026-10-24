from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/gitlens"
    app_env: str = "development"
    service_role: str = "web"
    worker_mode: str = "separate"
    auth_pepper: str = "dev-only-change-me"
    session_pepper: str = "dev-only-different-value"
    email_provider: str = "console"
    resend_api_key: str = ""
    email_from: str = "GitLens <login@example.com>"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_embedding_model: str = "gemini-embedding-2"
    embedding_dimensions: int = 768
    embedding_batch_size: int = 32
    summary_batch_size: int = 20
    max_summary_calls: int = 4
    index_schema_version: int = 2
    max_repo_map_chars: int = 12000
    max_tool_output_chars: int = 12000
    max_tool_file_lines: int = 200
    max_agent_turns: int = 4
    max_agent_tool_calls: int = 6
    github_token: str = ""
    github_oauth_client_id: str = ""
    github_oauth_client_secret: str = ""
    github_oauth_redirect_uri: str = "https://copoojjfbgbgcnaokahlbopnafkljfkf.chromiumapp.org/github"
    worker_dispatch_repo: str = ""
    worker_dispatch_ref: str = "main"
    worker_dispatch_token: str = ""
    extension_origin: str = ""
    admin_token: str = ""
    api_base_url: str = "http://127.0.0.1:8001"
    db_connect_attempts: int = 6
    db_connect_backoff_seconds: float = 0.5
    db_connect_timeout_seconds: int = 5
    worker_shutdown_timeout_seconds: float = 5
    archive_bytes: int = 50_000_000
    archive_entries: int = 10_000
    max_files: int = 2_000
    max_file_bytes: int = 256_000
    max_text_bytes: int = 20_000_000
    max_chunks_per_snapshot: int = 5_000
    max_symbol_references: int = 200_000
    max_chunks: int = 8
    max_context_chunks: int = 16
    max_context_chars: int = 56_000
    max_question_chars: int = 2_000
    max_history_turns: int = 2
    daily_user_snapshots: int = 2
    daily_user_chats: int = 20
    daily_global_snapshots: int = 20
    daily_global_chats: int = 200

    @model_validator(mode="after")
    def validate_production(self):
        if self.database_url.startswith("postgresql://"):
            self.database_url = self.database_url.replace("postgresql://", "postgresql+psycopg://", 1)
        if self.max_chunks < 1 or self.max_context_chunks < self.max_chunks or self.max_context_chars < 1 or self.max_chunks_per_snapshot < 1 or self.max_symbol_references < 1:
            raise ValueError("RAG retrieval limits are invalid")
        if self.embedding_dimensions != 768 or not 1 <= self.embedding_batch_size <= 100 or self.summary_batch_size < 1 or self.max_summary_calls < 1:
            raise ValueError("Repository index limits are invalid")
        if not 1 <= self.max_agent_turns <= 4 or not 1 <= self.max_agent_tool_calls <= 6 or self.max_tool_file_lines < 1 or self.max_tool_output_chars < 1000 or self.max_repo_map_chars < 1000:
            raise ValueError("Agent limits are invalid")
        if self.db_connect_attempts < 1 or self.db_connect_backoff_seconds < 0 or self.db_connect_timeout_seconds < 1 or self.worker_shutdown_timeout_seconds < 0:
            raise ValueError("Database retry and shutdown limits are invalid")
        if self.worker_mode not in {"separate", "inprocess", "actions"}:
            raise ValueError("WORKER_MODE must be separate, inprocess, or actions")
        if self.app_env == "production":
            expected_redirect = f"https://{self.extension_origin.removeprefix('chrome-extension://')}.chromiumapp.org/github"
            common = (bool(self.gemini_api_key), bool(self.github_token))
            web = (self.session_pepper != "dev-only-different-value", bool(self.github_oauth_client_id), bool(self.github_oauth_client_secret), self.github_oauth_redirect_uri == expected_redirect, bool(self.admin_token), self.extension_origin.startswith("chrome-extension://"))
            actions = (bool(self.worker_dispatch_repo), bool(self.worker_dispatch_ref), bool(self.worker_dispatch_token))
            if self.service_role == "web" and self.worker_mode == "actions":
                web += actions
            required = common if self.service_role == "worker" else common + web
            if not all(required):
                raise ValueError("Production credentials are incomplete for this service role")
        return self

settings = Settings()

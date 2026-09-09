import os
from dataclasses import dataclass

from dotenv import load_dotenv

from d1 import D1

load_dotenv()  # no-op in production (Cloud Run injects env vars directly, no .env file ships)


@dataclass
class Env:
    db: D1
    line_channel_secret: str
    line_channel_access_token: str
    anthropic_api_key: str
    openai_api_key: str
    liff_id: str
    scheduler_secret: str


_env: Env | None = None


def get_env() -> Env:
    global _env
    if _env is None:
        _env = Env(
            db=D1(
                account_id=os.environ["CF_ACCOUNT_ID"],
                database_id=os.environ["CF_D1_DATABASE_ID"],
                api_token=os.environ["CF_API_TOKEN"],
            ),
            line_channel_secret=os.environ["LINE_CHANNEL_SECRET"],
            line_channel_access_token=os.environ["LINE_CHANNEL_ACCESS_TOKEN"],
            anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
            openai_api_key=os.environ["OPENAI_API_KEY"],
            liff_id=os.environ.get("LIFF_ID", ""),
            scheduler_secret=os.environ.get("SCHEDULER_SECRET", ""),
        )
    return _env

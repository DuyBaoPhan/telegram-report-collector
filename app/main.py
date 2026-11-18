import asyncio
import logging

from app.bot.bot import run_bot
from app.config import Settings
from app.runtime import configure_logging, instance_lock


def main() -> None:
    try:
        settings = Settings.from_env()
        configure_logging(settings.log_level, settings.database_path, settings.bot_token)
        with instance_lock(settings.database_path):
            asyncio.run(run_bot(settings))
    except KeyboardInterrupt:
        print("Bot stopped.")
    except Exception as exc:
        # Even startup/auth/network exceptions may embed a token URL: never print
        # exception text or a traceback. Configuration contains no secret values.
        logging.getLogger("app.main").error("Bot stopped safely (%s). Check configuration, connectivity and local database access.", type(exc).__name__)
        print(f"Cannot start/continue bot ({type(exc).__name__}). Check configuration, connectivity and local database access.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

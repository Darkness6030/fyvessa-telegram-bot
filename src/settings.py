from pydantic import BaseModel, ConfigDict
from rewire import logger, simple_plugin

plugin = simple_plugin()


class SettingsValidationError(ValueError):
    pass


class StoreSettings(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    channel_url: str = ''
    reviews_channel_url: str = ''
    support_url: str = ''
    payment_details: str = ''


_settings = StoreSettings()


def get_settings() -> StoreSettings:
    return _settings


async def sync_settings() -> StoreSettings:
    from src.sheets import load_store_settings

    global _settings
    _settings = await load_store_settings()
    return _settings


@plugin.setup()
async def import_settings() -> None:
    try:
        await sync_settings()
    except SettingsValidationError as exc:
        logger.error('Settings setup sync failed: {}', exc)

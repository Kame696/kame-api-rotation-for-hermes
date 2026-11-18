"""1.9.0.3 (C4, designer answer 4) - what the user reads names the product API Rotation, not KAME."""
import re

from tests.test_1_8_2_9_speed_words import settings

BARE = re.compile(r"KAME(?![_\w])(?! API Rotation)")


def test_no_settings_title_or_help_says_kame():
    offenders = [(key, text) for key, entry in settings.META.items() for text in entry if isinstance(text, str) and BARE.search(text)]
    assert offenders == []


def test_the_provider_names_and_command_descriptions():
    from tests.test_1_8_4_2_listen import listen_mod, voice_mod
    assert listen_mod.DISPLAY_NAME == "API Rotation Gemini listening"
    assert voice_mod.DISPLAY_NAME == "API Rotation Gemini speech"
    assert listen_mod.PROVIDER_NAME == "kame-gemini", "the provider id is unchanged"

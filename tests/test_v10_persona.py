"""v10 (DONNA_PERSONA, TTS_SPEED base): the persona block and the speaking-rate base."""

from assistant.prompts import system_prompt
from assistant.speaking_rate import next_speed, sentence


def _prompt(**kw):
    return system_prompt("Donna", "King", 8.0, **kw)


def test_persona_off_by_default():
    p = _prompt()
    assert "Who you are" not in p
    assert "You are Donna, a voice assistant" in p


def test_persona_on():
    p = _prompt(persona=True)
    assert "the best assistant King has ever had" in p
    assert "{user_name}" not in p
    assert '"upset and negative"' in p  # tone rules use the Wav2Small hint words
    assert "Never mention a TV show" in p
    # the persona sits before the rules it must not override
    assert p.index("Who you are") < p.index("Everything you say is spoken aloud")
    assert p.index("Style never overrides") < p.index("Conversation rules")


def test_persona_hint_words_match_emotion():
    from assistant.emotion import WORDS

    p = _prompt(persona=True)
    for lo, hi in WORDS.values():
        assert f'"{lo}"' in p and f'"{hi}"' in p


def test_normal_speed_returns_to_base():
    assert next_speed(0.8, "normal", 0.95) == 0.95
    assert next_speed(0.95, "slower", 0.95) == 0.8
    assert next_speed(0.95, "normal") == 1.0  # old behaviour without a base
    assert sentence(0.95, 0.95, "normal") == "This is my normal speed."
    assert sentence(0.8, 0.95, "normal") == "Okay, back to my normal speed."

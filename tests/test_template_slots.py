"""The writer prompt's lines on the added records hold to the validator: the story examples fit the record size they
teach, the stated sizes are the validator's, the tone words it names are tones and not feelings, and a given record is
copied as given."""

import re

from thought_seeds.core import tp

from rps import rules

STORY = ("story before", "story after", "story cause")


def story_line(text):
    return next(line for line in text.splitlines() if line.startswith("- story before is"))


def test_the_story_examples_fit_the_record_size_they_teach():
    line = story_line(rules.writer_system())
    for key in STORY:
        example = re.search(key + r' (?:is )?[^"]*"([^"]+)"', line).group(1)
        assert len(key) + len(example) <= tp.REC_MAX, (key, example)
    room = {key: tp.REC_MAX - len(key) for key in STORY}
    assert room["story after"] == room["story cause"]
    assert f"story before at most {room['story before']} bytes, story after and story cause at most {room['story after']} " in line
    assert f"key and value together at most {tp.REC_MAX}" in line
    assert "never a moment word (instant before, instant after)" in line


def test_the_feelings_offered_are_printed_feelings_and_the_words_refused_are_not():
    from thought_seeds.core import HELD

    text = rules.writer_system()
    line = next(l for l in text.splitlines() if l.startswith("- mood feeling takes"))
    offered = re.search(r"the feeling is ([a-z, ]+) or (\w+);", line)
    offered = [w.strip() for w in offered.group(1).split(",")] + [offered.group(2)]
    refused = re.search(r"; (\w+), (\w+) and (\w+) are no feelings", line).groups()
    feeling = tp.VOCAB[tp.KEY_LIST["mood feeling"]]
    printed = tp.lists_for(HELD, "train")[tp.KEY_LIST["mood feeling"]]
    sealed = tp.sealed_all(HELD)
    assert len(offered) >= 5
    for word in offered:
        assert word in feeling and word in printed and word not in sealed, word
    for word in refused:
        assert word not in feeling, word


def test_a_given_record_is_copied_even_with_a_sealed_value():
    assert "A record or relation the item gives is copied as given, even when it holds one of these values." in rules.writer_system()

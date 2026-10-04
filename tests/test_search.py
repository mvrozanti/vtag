import fuzzymatch
import searchquery
from webui.cache import _doc, _term_scorer


def doc(path, tags=(), labels=()):
    payload = {"tags": list(tags), "user_labels": list(labels), "source": {"sha256": "ab" * 32}}
    return _doc(path, 0, payload, ("/memes",))


def hit(query, d):
    return searchquery.compile_query(query, _term_scorer)(d)


def test_squash_ignores_separators():
    assert fuzzymatch.squash("Pepe_the-Frog.jpg") == "pepethefrogjpg"


def test_separators_do_not_matter_for_filenames():
    d = doc("/memes/pepe_frog.jpg")
    for q in ("pepefrog", "pepe-frog", "pepe.frog", '"pepe frog"'):
        h = hit(q, d)
        assert h is not None and not h.fuzzy


def test_separators_do_not_matter_for_tags_but_tags_stay_separate():
    d = doc("/memes/x.jpg", tags=["two buttons", "sweat"])
    assert hit("twobuttons", d) is not None
    assert hit("buttonssweat", d) is None


def test_reldir_is_searchable():
    assert hit("todayifeel", doc("/memes/today-i-feel/a.mp4")) is not None


def test_fuzzy_filename_abbreviations_and_typos():
    for q, name in (("japburro", "japonesa-burro.mp4"), ("racoon", "raccoon-12.jpg"),
                    ("bikecrash", "Insane 3 Bikers Crash.mp4")):
        h = hit(q, doc(f"/memes/{name}"))
        assert h is not None and h.fuzzy, (q, name)


def test_fuzzy_rejects_scattered_matches():
    assert hit("pepe", doc("/memes/Wash_Your_Penis_by_Jordan_B._Peterson.mp4")) is None
    assert hit("1489", doc("/memes/1004080912.jpg")) is None


def test_exact_outranks_fuzzy_and_name_outranks_tags():
    by_name = hit("raccoon", doc("/memes/raccoon.jpg"))
    by_tag = hit("raccoon", doc("/memes/x.jpg", tags=["raccoon"]))
    fuzzy = hit("racoon", doc("/memes/raccoon.jpg"))
    assert by_name.score > by_tag.score > fuzzy.score


def test_boolean_grammar():
    d = doc("/memes/smug-pepe.png", tags=["green"])
    assert hit("pepe AND green", d) is not None
    assert hit("pepe NOT green", d) is None
    assert hit("wojak OR smug", d) is not None
    assert hit("(wojak OR smug) NOT apu", d) is not None

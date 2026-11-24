import wording
from webui.cache import Snapshot, _doc


def doc(path, ocr=(), mtime=0):
    payload = {"tags": [], "text_ocr": list(ocr), "source": {"sha256": path.encode().hex()[:64]}}
    return _doc(path, mtime, payload, ("/memes",))


def search(query, *docs):
    snap = Snapshot(docs=list(docs), by_id={d.id: d for d in docs}, tagged=len(docs), last_mtime=0)
    items, total, _ = snap.search(query=query)
    return [i["path"] for i in items]


def test_expand_spells_out_contractions_and_leaves_words_alone():
    assert wording.expand("I’m going INSANE") == "i am going insane"
    assert wording.expand("dont stop") == "do not stop"
    assert wording.expand("imagine him") == "imagine him"
    assert wording.expand("its fine, ill go") == "its fine, ill go"


def test_exact_phrase_outranks_scattered_short_terms():
    scattered = doc("/memes/img_1984.jpg", ["damn insane reality"], mtime=2)
    phrase = doc("/memes/cat.jpg", ["i am insane"], mtime=1)
    assert search("i am insane", scattered, phrase)[0] == phrase.path


def test_contractions_find_the_spelled_out_caption_and_back():
    spelled = doc("/memes/cat.jpg", ["i am insane"])
    contracted = doc("/memes/sign.jpg", ["(i'm going insane)"])
    for query in ("i'm insane", "im insane", "i’m insane"):
        assert spelled.path in search(query, spelled, contracted), query
    assert search("i am going insane", spelled, contracted) == [contracted.path]


def test_single_letters_weigh_less_than_whole_words():
    by_letter = doc("/memes/i.jpg", ["nothing"])
    by_word = doc("/memes/x.jpg", ["insane"])
    assert search("i OR insane", by_letter, by_word)[0] == by_word.path

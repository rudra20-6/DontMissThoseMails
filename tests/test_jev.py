from app.clients.jev import Answers, Choice, Noul, Score

LEVELS = ["a", "b", "c", "d", "e"]


def test_score_from_probability_list():
    a = Answers(raw={"s": {"type": "score", "probabilities": [0, 0, 0, 1, 0]}}, questions={"s": Score("x", LEVELS)})
    assert a.score("s") == 3


def test_score_from_probability_dict_and_raw():
    a = Answers(raw={"s": {"probabilities": {"a": 0.5, "e": 0.5}}}, questions={"s": Score("x", LEVELS)})
    assert a.score("s") == 2
    b = Answers(raw={"s": {"score": 2.7}}, questions={"s": Score("x", LEVELS)})
    assert b.score("s") == 2.7


def test_choice_and_noul():
    q = {"c": Choice("x", {"one": None, "two": "desc"}), "n": Noul("y")}
    a = Answers(raw={"c": {"choice": "two", "probabilities": {"one": 0.2, "two": 0.8}}, "n": {"noul": 0.9}}, questions=q)
    assert a.choice("c") == ("two", 0.8)
    assert a.noul("n") == 0.9

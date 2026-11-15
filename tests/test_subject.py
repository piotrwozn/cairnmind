"""The subject check of an answer (issue #245): the names a question uses for its subject must be
in a quoted source, or the answer is not complete, whatever the judging model says.

The product names here are deliberately not those of docs/evaluation/absent-questions.json: the
repository evaluation indexes the tests, and a test that put one of those names beside a port
number would be found for the very question that must stay without an answer.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cairnmind.agent import KnowledgeAgent
from cairnmind.repository_evaluation import load_questions
from cairnmind.subject import SUBJECT_GAP, mentions, missing_subject, names_directory, subject_terms

from test_knowledge import FakeOllama, document, final, workspace  # noqa: F401 - workspace is a fixture

REPOSITORY = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("question, terms", [
    # A proper name inside the sentence, however many generic words surround it.
    ("What is the default Zookeeper port used by this project?", ["Zookeeper"]),
    ("Which Consul version does the service need?", ["Consul"]),
    # Identifiers: an underscore, a file name, a capital inside, letters with digits.
    ("What is the default value of jaeger_endpoint in the settings?", ["jaeger_endpoint"]),
    ("Which version does setup.cfg declare?", ["setup.cfg"]),
    ("At which address is CockroachDB expected by default?", ["CockroachDB"]),
    ("Which model is used, mistral7 or another?", ["mistral7"]),
    # One word alone between backticks is a name even in lower case; a command of several words is not.
    ("What is the default `etcd` port?", ["etcd"]),
    ("What does `example status` print first?", []),
    # No name at all: the gate does not apply.
    ("On which port does the local service listen by default?", []),
    ("How many files does indexing write in one transaction at most?", []),
    ("What is the default timeout of a question, in seconds?", []),
    # The word that begins a sentence is capitalised by the language, not by being a name.
    ("Timeouts are how long by default? Which default applies?", []),
    # Generic words stay generic when capitalised or in backticks; so do abbreviations in capitals.
    ("What does the Settings class set as the `port` of the API and the URL of the GPU server?", []),
    ("What is the default TTL of a cached row?", []),
    # Numbers and versions are not names.
    ("On which card were the results for 0.8.0 measured with 5,000 files?", []),
    # A pasted value (a token, a hash, a commit, an id) is not a name.
    ("Where is authenticate? token Zx3kQ9_vB7-mN2pL5rT8wY1cF4hJ6dG0sA", []),
    ("What did commit d489cee change, and what is 7f3a9b2c-11de-4a5b-9c3d-2e1f0a9b8c7d?", []),
    # A lower-case product name without backticks has no shape to go by: a known limit.
    ("What is the default haproxy port?", []),
    # Each name once, in order.
    ("Does Consul talk to Vault, and does Vault answer Consul?", ["Consul", "Vault"]),
])
def test_the_names_of_a_question_s_subject_are_read_from_their_written_shape(question, terms):
    assert subject_terms(question) == terms


def test_the_project_s_own_names_are_not_a_subject():
    assert subject_terms("Which Consul version does Widgetlib need?", ["widgetlib"]) == ["Consul"]
    assert subject_terms("Which port does Widget_Lib use?", ["widget-lib"]) == []
    assert names_directory("Widgetlib", ["/src/widgetlib/config.py"])
    assert not names_directory("Widgetlib", ["/src/widgetlib.py", "/docs/widgetlib-guide/a.md"])


@pytest.mark.parametrize("term, text, found", [
    ("Zookeeper", "zookeeper_port: int = 2181", True),            # case, a part of an identifier
    ("Zookeeper", "self.zookeeperPort = 2181", True),              # a part of a camel-case identifier
    ("Zookeeper", "ZOOKEEPER_PORT=2181", True),
    ("Zookeeper", "two zookeepers and the zookeeper's port", True),  # plural and possessive
    ("Zookeeper", "zoo_keeper: 2181", True),                        # written in two parts
    ("Zookeeper", "port: int = 8765", False),
    ("Zookeeper", "the bookkeeper port", False),                    # inside another word
    ("Consul", "the consulate and a consultant", False),
    ("Consul", "docs/consul/README.md", True),
    ("CockroachDB", "cockroach_url: str = 'x'", True),              # a compound shortened to its first part
    ("CockroachDB", "Cockroach DB 23", True),
    ("CockroachDB", "cockroaches", False),
    ("JavaScript", "java_home", False),                             # a short first part does not stand for the name
    ("jaeger_endpoint", "JAEGER-ENDPOINT", True),                   # underscore, hyphen, case
    ("jaeger_endpoint", "jaegerEndpoint = None", True),
    ("jaeger_endpoint", "jaeger = None  # endpoint", False),
    ("setup.cfg", "see setup.cfg", True),
    ("mistral7", "mistral7:latest", True),
    ("mistral7", "mistral 7", True),
    ("mistral7", "mistral", False),
    ("Consula", "consul_port = 8500", True),                        # an inflected name (Polish genitive)
    ("Javy", "It needs Java 21+", True),                            # the final vowel of an inflected name
    ("Java", "a JVM option", False),
])
def test_a_name_matches_across_case_separators_compounds_and_endings(term, text, found):
    assert mentions(term, text) is found


def test_every_name_must_be_somewhere_and_a_question_without_names_misses_nothing():
    passages = ["zookeeper_port: int = 2181", "src/example/config.py"]
    assert missing_subject("What is the default Zookeeper port?", passages) == []
    assert missing_subject("What is the default Zookeeper port of Consul?", passages) == ["Consul"]
    assert missing_subject("What is the default port?", []) == []
    # An oversized or hostile question costs bounded work and never raises: a word longer than any
    # name is not one, and only the first names are checked.
    assert missing_subject("A " + "Consul" * 50000 + " (((" + "\\" * 50, passages) == []
    assert subject_terms("Is Supercalifragilistic" + "Expialidocious" * 5 + " set?") == []
    assert len(subject_terms(" ".join(f"x Name{index}" for index in range(100)))) == 8


def test_the_absent_questions_name_their_subject_except_the_two_that_are_marked_as_such():
    cases = load_questions(REPOSITORY / "docs" / "evaluation" / "absent-questions.json", REPOSITORY)
    named = [bool(subject_terms(case["question"], ["cairnmind"])) for case in cases]
    assert named == [True] * (len(cases) - 2) + [False, False] and len(cases) >= 12


CONFIG = "class Settings:\n    host: str = \"127.0.0.1\"\n    port: int = 8765\n    cockpit_port: int = 8766\n"


async def _asked(store, project, settings, question, text=CONFIG, path="src/example/config.py", answer="8765",
                 quote="port: int = 8765"):
    uri = str(Path(project["root"]) / path)
    source, _ = document(store, project, text, uri=uri, metadata={"relative_path": path})
    model = FakeOllama([final(source["id"], quote, answer)])  # the judge accepts whatever it is shown
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], question)
    return result, model


async def test_an_answer_about_a_thing_the_quoted_source_never_names_is_not_complete(workspace):  # noqa: F811
    # The trial's finding: the model answered a question about a product the project does not
    # have with the service's own port, and the judge accepted the quote that shares the number.
    store, project, settings = workspace
    result, model = await _asked(store, project, settings,
                                 "What is the default Zookeeper port used by this project?")
    assert result["status"] == "partial" and result["diagnostics"]["code"] == "answer_unsupported"
    assert "8765" not in result["answer"] and "8765" not in json.dumps(result["unknowns"])
    assert result["unknowns"][-1] == SUBJECT_GAP + "Zookeeper"
    assert result["diagnostics"]["support_check"] == "subject_missing"
    assert not model.support_calls  # refused without asking the judge
    assert [source["quote"] for source in result["sources"]] == ["port: int = 8765"]  # the evidence stays visible


async def test_a_question_without_a_name_goes_to_the_judge_as_before(workspace):  # noqa: F811
    store, project, settings = workspace
    result, model = await _asked(store, project, settings, "On which port does the local service listen by default?")
    assert result["status"] == "complete" and result["answer"] == "8765"
    assert result["diagnostics"]["support_check"] == "supported" and len(model.support_calls) == 1


@pytest.mark.parametrize("text, path, quote", [
    (CONFIG + "    zookeeper_port: int = 8765\n", "src/example/config.py", "zookeeper_port: int = 8765"),  # in the quote
    (CONFIG, "deploy/zookeeper/settings.py", "port: int = 8765"),                                   # in the path
    ("# Zookeeper\n\nThe coordination service.\n\n" + CONFIG, "src/example/config.py", "port: int = 8765"),  # elsewhere in the source
])
async def test_a_name_in_the_quote_its_path_or_the_rest_of_its_source_lets_the_judge_decide(
        workspace, text, path, quote):  # noqa: F811
    store, project, settings = workspace
    result, model = await _asked(store, project, settings, "What is the default Zookeeper port?",
                                 text=text, path=path, quote=quote)
    assert result["status"] == "complete" and len(model.support_calls) == 1


async def test_the_project_s_own_name_and_its_directories_are_never_missing(workspace):  # noqa: F811
    store, project, settings = workspace
    # "Example" is the registered name of the project; "Widgetlib" is a directory of its sources.
    document(store, project, "VERSION = 1\n", uri=str(Path(project["root"]) / "src/widgetlib/about.py"))
    for question in ("On which port does Example listen by default?",
                     "On which port does Widgetlib listen by default?"):
        result, model = await _asked(store, project, settings, question, path="settings/config.py")
        assert result["status"] == "complete", question
    # A directory outside the project's root is not a part of the project.
    document(store, project, "VERSION = 2\n", uri="/elsewhere/gadgetlib/about.py")
    result, _ = await _asked(store, project, settings, "On which port does Gadgetlib listen by default?",
                             path="settings/config.py")
    assert result["status"] == "partial" and result["diagnostics"]["support_check"] == "subject_missing"


def test_the_gap_of_a_refused_subject_is_translated_and_the_names_are_kept():
    from cairnmind.terminal_i18n import tr
    from cairnmind.terminal_i18n_messages import ASK_SUBJECT_PREFIX, fixed_message
    assert ASK_SUBJECT_PREFIX == SUBJECT_GAP
    polish = fixed_message(SUBJECT_GAP + "Zookeeper, [bold]Consul", lambda text: tr(text, "pl"))
    assert polish == "Cytowane źródła nie wymieniają tego, o co pyta pytanie: Zookeeper, [bold]Consul"
    assert fixed_message(SUBJECT_GAP + "Zookeeper", lambda text: tr(text, "en")) == SUBJECT_GAP + "Zookeeper"

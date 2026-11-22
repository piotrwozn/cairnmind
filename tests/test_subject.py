"""The subject check of an answer (issue #245): the names a question uses for its subject must be
in a quoted source, or the answer is not complete, whatever the judging model says.

The product names here are deliberately not those of docs/evaluation/absent-questions.json: the
repository evaluation indexes the tests, and a test that put one of those names beside a port
number would be found for the very question that must stay without an answer.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from cairnmind.agent import KnowledgeAgent
from cairnmind.repository_evaluation import load_questions
from cairnmind.ollama import OllamaUnavailable
from cairnmind.subject import (NAMES_GAP, SUBJECT_GAP, _matcher, inflecting, mentions, missing_subject, names_directory,
                               subject_terms, unnamed_answer_names, unnamed_subject)

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
    # The word that begins a sentence is capitalised by the language: a name there unless it is a
    # word that opens a question or a request, or the question also writes it in lower case.
    ("Timeouts are how long by default? Which default applies?", []),
    ("Zookeeper listens on which port by default?", ["Zookeeper"]),
    ("Consul port: what is the default?", ["Consul"]),
    ("Vault's default port is what?", ["Vault"]),
    ("Quick question: Consul uses which port by default?", ["Consul"]),
    ("Consul ma domyślnie jaki port?", ["Consul"]),
    ("Under which licence is it published? Show the largest file. Largest of all? Please list them.", []),
    ("Adres usługi? Jaki jest domyślny port? Podaj wersję.", []),
    ("Journal keeps how many rows? The journal is trimmed.", []),
    ("Hi. Is it set? On which host?", []),
    # Generic words stay generic when capitalised or in backticks; so do abbreviations in capitals.
    ("What does the Settings class set as the `port` of the API and the URL of the GPU server?", []),
    ("What is the default TTL of a cached row?", []),
    # Numbers and versions are not names.
    ("On which card were the results for 0.8.0 measured with 5,000 files?", []),
    # A pasted value (a token, a hash, a commit, an id) is not a name.
    ("Where is authenticate? token Zx3kQ9_vB7-mN2pL5rT8wY1cF4hJ6dG0sA", []),
    ("What did commit d489cee change, and what is 7f3a9b2c-11de-4a5b-9c3d-2e1f0a9b8c7d?", []),
    # Days and months are capitalised by the language; in headline case every word is.
    ("What changed since Monday in the November notes?", []),
    ("What Is The Default Zookeeper Port Of The Local Service?", []),
    # A lower-case product name without backticks has no shape to go by: a known limit. So is a
    # name in capitals, which are an abbreviation or emphasis as often as a name.
    ("What is the default haproxy port?", []),
    ("What is the default ZOOKEEPER port, REALLY?", []),
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
    ("CockroachDB", "cockroach_url: str = 'x'", False),             # a part of a compound is not the name
    ("CockroachDB", "a cockroach in the kitchen", False),
    ("GraphStore", "a knowledge graph of records and GraphRows", False),
    ("LevelStore", "the log level", False),
    ("Stripe", "value.strip()", False),                             # the last letter belongs to the name
    ("Maria", "image: mariadb:11", True),                           # db, sql, ql, js written after the name
    ("Presto", "prestosql://host", True),
    ("Arcade", "arcadedata/arcadedb", True),
    ("Maria", "marianne and mariachi", False),
    ("compose.yaml", "docker-compose.yml", True),                   # one ending, two spellings
    ("compose.yml", "docker-compose.yaml", True),
    ("compose.yaml", "compose.toml", False),
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
    ("Java", "a JVM option", False),
])
def test_a_name_matches_across_case_separators_compounds_and_endings(term, text, found):
    assert mentions(term, text) is found


@pytest.mark.parametrize("term, text", [
    ("Consula", "consul_port = 8500"),      # genitive
    ("Javy", "It needs Java 21+"),          # the final vowel changed
    ("Consulem", "consul_port = 8500"),     # instrumental
    ("Consulu", "the Consul agent"),        # locative
    ("Vaultcie", "vault_address"),          # locative with a softened ending
    ("Ollamie", "ollama_url"),              # locative of a name that ends in a vowel
])
def test_a_case_ending_is_dropped_only_for_a_question_in_an_inflecting_language(term, text):
    assert mentions(term, text, inflected=True) and not mentions(term, text)
    assert inflecting("Jaki model jest używany w Ollamie?") and inflecting("Jaka wersja jest wymagana?")
    assert not inflecting("Which Jest version is used on the pod?")
    config = ["consul_port: int = 8500", "src/example/config.py"]
    assert missing_subject("Jaki port jest domyślny w Consulu?", config) == []
    assert missing_subject("Jaki port ma Consulem zarządzana usługa?", config) == []
    assert missing_subject("What is the default port of Consulem?", config) == ["Consulem"]


def test_an_identifier_is_found_without_a_prefix_that_is_the_project_s_or_stands_alone_in_the_source():
    # A settings module reads EXAMPLE_PORT as env("PORT") with a prefix; the documented name is the long one.
    source = ('ENV_PREFIXES = ("EXAMPLE_", "OLDNAME_")\nport = env_value("PORT", "8765")\n'
              'store_url = env_value("STORE_URL")\n')
    passages = ['port = env_value("PORT", "8765")', "src/example/config.py", source]
    assert missing_subject("What does EXAMPLE_PORT default to?", passages) == []
    assert missing_subject("What is the default of OLDNAME_STORE_URL?", passages) == []
    assert missing_subject("What is the default of EXAMPLE_QUEUE_URL?", passages) == ["EXAMPLE_QUEUE_URL"]
    assert missing_subject("What is the default of OTHER_STORE_URL?", passages) == ["OTHER_STORE_URL"]
    assert missing_subject("What is the default value of store_port in the settings?", passages) == ["store_port"]
    # Without the prefix in the source, the project's own name still counts as one.
    bare = ['store_url = env_value("STORE_URL")', "src/example/config.py"]
    assert missing_subject("What is the default of WIDGETLIB_STORE_URL?", bare) == ["WIDGETLIB_STORE_URL"]
    assert missing_subject("What is the default of WIDGETLIB_STORE_URL?", bare, ["widgetlib"]) == []


def test_every_name_must_be_somewhere_and_a_question_without_names_misses_nothing():
    passages = ["zookeeper_port: int = 2181", "src/example/config.py"]
    assert missing_subject("What is the default Zookeeper port?", passages) == []
    assert missing_subject("What is the default Zookeeper port of Consul?", passages) == ["Consul"]
    assert missing_subject("What is the default port?", []) == []
    assert missing_subject("Consul listens on which port by default?", passages) == ["Consul"]
    # However many names a question has, each is needed: one that the source lacks may be the
    # subject, and two that it has do not make the third one present.
    assert missing_subject("What port do Consul, Zookeeper and Example use by default?",
                           [*passages, "the Example service"]) == ["Consul"]
    rich = "With Consul behind Vault on Debian I see zookeeper_port refused; what is its default?"
    assert subject_terms(rich) == ["Consul", "Vault", "Debian", "zookeeper_port"]
    assert missing_subject(rich, [*passages, "the Consul agent"]) == ["Vault", "Debian"]
    assert missing_subject(rich, [*passages, "Consul and Vault on Debian"]) == []


def test_an_oversized_or_hostile_question_costs_bounded_work_and_never_raises():
    passages = ["zookeeper_port: int = 2181", "src/example/config.py"]
    # A word longer than any name is not one, and only the first names are checked.
    assert missing_subject("A " + "Consul" * 50000 + " (((" + "\\" * 50, passages) == []
    assert subject_terms("Is Supercalifragilistic" + "Expialidocious" * 5 + " set?") == []
    assert len(subject_terms(" ".join(f"x Name{index}" for index in range(100)))) == 8
    # A long name is looked for whole, not split at every letter; every name is one compiled pattern.
    long_name = "Zz" + "a" * 60
    assert subject_terms(f"What about {long_name} here?") == [long_name]
    assert _matcher(long_name, False)[1].pattern.count("zz") == 2
    assert _matcher("Zookeeper", False)[1].pattern.count("zoo") == 6
    # Large sources with none of the names are passed over by their first letters.
    big = "lorem ipsum dolor sit amet consectetur " * 30000
    names = " and ".join("Zz" + chr(97 + index) * 60 for index in range(8))
    started = time.perf_counter()
    assert len(missing_subject(f"What about {names} here?", [big, big, big])) == 8
    assert time.perf_counter() - started < 5


def test_the_absent_questions_name_their_subject_except_the_three_that_are_marked_as_such():
    cases = load_questions(REPOSITORY / "docs" / "evaluation" / "absent-questions.json", REPOSITORY)
    named = [bool(subject_terms(case["question"], ["cairnmind"])) for case in cases]
    assert named == [True] * (len(cases) - 3) + [False, False, False] and len(cases) >= 16


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


@pytest.mark.parametrize("question, unnamed", [
    ("Zookeeper listens on which port by default?", "Zookeeper"),                     # the name begins the sentence
    ("What port do Zookeeper and Example use by default, as config.py sets it?", "Zookeeper"),  # beside names that are there
])
async def test_the_name_is_read_wherever_it_stands_and_other_names_do_not_cover_for_it(
        workspace, question, unnamed):  # noqa: F811
    store, project, settings = workspace
    result, model = await _asked(store, project, settings, question)
    assert result["status"] == "partial" and result["unknowns"][-1] == SUBJECT_GAP + unnamed
    assert result["diagnostics"]["support_check"] == "subject_missing" and not model.support_calls


async def test_a_setting_asked_with_the_project_s_prefix_is_found_without_it(workspace):  # noqa: F811
    # "Example" is the registered name of the project; its settings file says only "port".
    store, project, settings = workspace
    result, _ = await _asked(store, project, settings, "On which port does the local service listen by default?")
    source = store.query("SELECT FROM sources WHERE id=:id", {"id": result["sources"][0]["id"]})[0]
    asked = [store, project["id"], "What does EXAMPLE_PORT default to?", result["sources"], {"S1": source}]
    assert unnamed_subject(*asked) == []
    asked[2] = "What does ZOOKEEPER_PORT default to, and EXAMPLE_HEARTBEAT?"
    assert unnamed_subject(*asked) == ["ZOOKEEPER_PORT", "EXAMPLE_HEARTBEAT"]


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


async def test_a_question_asked_with_an_answer_schema_is_a_caller_s_instruction_and_is_not_read(workspace):  # noqa: F811
    # The recall evaluation and the agents ask this way: the question names the fields to fill
    # ("answer_schema", "next_action"), which no source has to repeat.
    store, project, settings = workspace
    uri = str(Path(project["root"]) / "src/example/config.py")
    source, _ = document(store, project, CONFIG, uri=uri, metadata={"relative_path": "src/example/config.py"})
    contract = {"type": "object", "properties": {"port": {"type": "integer"}}, "required": ["port"],
                "additionalProperties": False}
    model = FakeOllama([final(source["id"], "port: int = 8765", {"port": 8765})])
    result = await KnowledgeAgent(store, settings, model).ask(
        project["id"], "Retrieve the service port. Put an object matching the caller-provided answer_schema "
                       "in the answer field, with exactly the field service_port_value.", answer_schema=contract)
    assert result["status"] == "complete" and len(model.support_calls) == 1


def test_the_gap_of_a_refused_subject_is_translated_and_the_names_are_kept():
    from cairnmind.terminal_i18n import tr
    from cairnmind.terminal_i18n_messages import ASK_SUBJECT_PREFIX, fixed_message
    assert ASK_SUBJECT_PREFIX == SUBJECT_GAP
    polish = fixed_message(SUBJECT_GAP + "Zookeeper, [bold]Consul", lambda text: tr(text, "pl"))
    assert polish == "Cytowane źródła nie wymieniają tego, czego dotyczy pytanie: Zookeeper, [bold]Consul"
    assert fixed_message(SUBJECT_GAP + "Zookeeper", lambda text: tr(text, "en")) == SUBJECT_GAP + "Zookeeper"


def test_the_gaps_of_the_names_checks_are_translated_and_the_names_are_kept():
    from cairnmind.terminal_i18n import tr
    from cairnmind.terminal_i18n_messages import ASK_NAMES_PREFIX, fixed_message
    assert ASK_NAMES_PREFIX == NAMES_GAP
    polish = fixed_message(NAMES_GAP + "nginx, [bold]Redis", lambda text: tr(text, "pl"))
    assert polish == "Cytowane źródła nie podają nazw, które podaje odpowiedź: nginx, [bold]Redis"
    unreadable = "Could not check the names the answer gives."
    assert fixed_message(unreadable, lambda text: tr(text, "pl")) == "Nie udało się sprawdzić nazw, które podaje odpowiedź."


# The names the checks must pass, the shapes the review named as costs (the subject check withholds a right
# answer when no quoted source has a name that is only context) and three that no check of shapes reads.
SUBJECT_SET = REPOSITORY / "docs" / "evaluation" / "subject-questions.json"
WITHHELD = {"context-operating-system": ["Fedora"], "context-person": ["Anna"],
            "context-two-names": ["Fedora", "Anna"], "word-first-in-the-sentence": ["Roughly"],
            "polish-infinitive": ["Sprawdzić"]}
CONTROLS = ["service-port", "name-the-file-has", "java-is-a-name", "polish-request"]


def test_what_the_subject_check_withholds_on_the_question_set_built_for_it_is_counted_without_a_model():
    # No model, no engine: the question loads like the repository questions, and for each one that names a
    # file the check is run against what a model that quoted the right file would have: its text and its path.
    cases = {case["id"]: case for case in load_questions(SUBJECT_SET, REPOSITORY)}
    withheld = {}
    for case in cases.values():
        if case["file"]:
            text = (REPOSITORY / case["file"]).read_text(encoding="utf-8")
            missing = missing_subject(case["question"], [text, case["file"]], ["cairnmind"])
            if missing:
                withheld[case["id"]] = missing
    assert withheld == WITHHELD and all(case in cases for case in CONTROLS)
    # N of M right-answer questions of the cost shapes: all of them, by the way they were chosen; none of the controls.
    assert len(withheld) == 5 and len(cases) - 3 - len(CONTROLS) == 5
    # The three about things the project does not have name nothing the check can read, except a real name.
    absent = {case["id"]: subject_terms(case["question"], ["cairnmind"]) for case in cases.values() if case.get("absent")}
    assert absent == {"lower-case-name": [], "headline-case-name": [], "real-name-other-thing": ["Ollama"]}


async def test_a_lower_case_name_the_quotes_do_not_state_is_withheld_by_the_names_call(workspace):  # noqa: F811
    # The shape no check reads: nginx in lower case. The judge accepts the quote that shares the number,
    # the subject check sees no name, and the answer would be complete and wrong. The names call stops it.
    store, project, settings = workspace
    reply = {"content": json.dumps({"names": [{"name": "nginx", "stated": False}, {"name": "Cairnmind", "stated": True}]})}
    uri = str(Path(project["root"]) / "src/example/config.py")
    source, _ = document(store, project, CONFIG, uri=uri, metadata={"relative_path": "src/example/config.py"})
    model = FakeOllama([final(source["id"], "port: int = 8765", "The service listens on 8765 behind nginx.")],
                       names=reply)
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "On which port does the service listen?")
    assert result["status"] == "partial" and result["diagnostics"]["code"] == "answer_unsupported"
    assert "8765" not in result["answer"] and "nginx" not in result["answer"]  # withheld, not rewritten
    assert result["unknowns"][-1] == NAMES_GAP + "nginx"
    assert result["diagnostics"]["support_check"] == "names_unstated"
    assert [call["phase"] for call in result["diagnostics"]["model_calls"]] == ["discovery", "support", "names"]
    assert [source["quote"] for source in result["sources"]] == ["port: int = 8765"]


async def test_a_right_answer_whose_names_the_quotes_state_stays_complete_and_the_call_treats_it_as_data(workspace):  # noqa: F811
    store, project, settings = workspace
    result, model = await _asked(store, project, settings, "On which port does the local service listen by default?")
    assert result["status"] == "complete" and result["diagnostics"]["support_check"] == "supported"
    call, = model.names_calls
    assert call["tools"] is None and call["kwargs"]["format"]["required"] == ["names"]
    assert "UNTRUSTED DATA" in call["messages"][0]["content"]
    payload = json.loads(call["messages"][1]["content"])
    assert payload["answer"] == "8765" and payload["citations"][0]["quote"] == "port: int = 8765"
    assert "question" not in payload  # the names are the answer's, not the question's


@pytest.mark.parametrize("names", [
    OllamaUnavailable("names down"),
    {"content": "no names"},
    {"content": json.dumps({"names": "nginx"})},
    {"content": json.dumps({"names": [{"name": "nginx"}]})},
    {"content": json.dumps({"names": []}), "tool_calls": [{"function": {"name": "x", "arguments": {}}}]},
])
async def test_a_failed_or_unreadable_names_call_withholds_the_answer_and_says_why(workspace, names):  # noqa: F811
    store, project, settings = workspace
    uri = str(Path(project["root"]) / "src/example/config.py")
    source, _ = document(store, project, CONFIG, uri=uri, metadata={"relative_path": "src/example/config.py"})
    model = FakeOllama([final(source["id"], "port: int = 8765", "8765")], names=names)
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "On which port does the service listen?")
    assert result["status"] == "partial" and result["diagnostics"]["code"] == "support_unverified"
    assert "8765" not in result["answer"] and result["unknowns"][-1] == "Could not check the names the answer gives."
    assert result["diagnostics"]["support_check"] == "names_failed" and len(model.names_calls) == 1


async def test_the_names_call_is_made_only_for_a_complete_answer_the_judge_accepted(workspace):  # noqa: F811
    store, project, settings = workspace
    uri = str(Path(project["root"]) / "src/example/config.py")
    source, _ = document(store, project, CONFIG, uri=uri, metadata={"relative_path": "src/example/config.py"})
    quote = "port: int = 8765"
    # An unknown answer is partial; a refused subject and a judge's no end before the call; so does a typed question.
    unknown = FakeOllama([final(source["id"], quote, "unknown", ["Not stated."])] * 2)
    refused = FakeOllama([final(source["id"], quote)])
    judged = FakeOllama([final(source["id"], quote)], support={"content": json.dumps({"supported": False, "reason": "No."})})
    typed = FakeOllama([final(source["id"], quote, {"port": 8765})])
    contract = {"type": "object", "properties": {"port": {"type": "integer"}}, "required": ["port"],
                "additionalProperties": False}
    for model, question, schema in ((unknown, "On which port?", None), (refused, "What is the Zookeeper port?", None),
                                    (judged, "On which port?", None), (typed, "Retrieve the port.", contract)):
        await KnowledgeAgent(store, settings, model).ask(project["id"], question, answer_schema=schema)
        assert model.names_calls == []
    assert typed.support_calls and not unknown.support_calls


async def test_the_names_an_answer_introduces_must_be_in_a_quote_or_a_path_once_the_check_is_on(
        workspace, monkeypatch):  # noqa: F811
    store, project, settings = workspace
    monkeypatch.setattr("cairnmind.subject.CHECK_ANSWER_NAMES", True)
    for answer, status in (("The project uses Zookeeper on 8765.", "partial"), ("The port is 8765.", "complete")):
        result, model = await _asked(store, project, settings, "On which port does the local service listen by default?",
                                     answer=answer)
        assert result["status"] == status
        if status == "partial":
            assert result["unknowns"][-1] == NAMES_GAP + "Zookeeper" and not model.support_calls
            assert result["diagnostics"]["support_check"] == "subject_missing" and "8765" not in result["answer"]
    # The same answer with the check off (the default until the replay) is left to the two model calls.
    monkeypatch.setattr("cairnmind.subject.CHECK_ANSWER_NAMES", False)
    result, _ = await _asked(store, project, settings, "On which port does the local service listen by default?",
                             answer="The project uses Zookeeper on 8765.")
    assert result["status"] == "complete"


async def test_a_name_of_the_question_the_project_or_a_quote_or_path_has_is_not_one_the_answer_introduces(workspace):  # noqa: F811
    store, project, settings = workspace
    result, _ = await _asked(store, project, settings, "On which port does the local service listen by default?")
    sources = result["sources"]
    ask = lambda question, answer: unnamed_answer_names(store, project["id"], question, answer, sources)  # noqa: E731
    assert ask("On which port?", "Zookeeper listens on 8765") == ["Zookeeper"]
    assert ask("On which port does Zookeeper listen?", "Zookeeper listens on 8765") == []   # the question has it
    assert ask("On which port?", "Example listens on 8765") == []                           # the project's own name
    assert ask("On which port?", "It is set in config.py") == []                            # the path has config.py
    assert ask("On which port?", "listens on 8765, as the quote says: port") == []          # no name at all
    assert ask("Na jakim porcie?", "Na porcie 8765 słucha Zookeeper.") == ["Zookeeper"]

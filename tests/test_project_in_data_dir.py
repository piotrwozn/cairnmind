"""A folder inside the data directory is never indexed, so it is refused when registered (#295)."""
import pytest

from cairnmind.store import Store


def test_a_folder_inside_the_data_directory_is_refused_by_every_way_of_registering_it(tmp_path):
    store = Store(tmp_path / 'data')
    project = store.add_project('safe', tmp_path / 'safe')
    for inside in (store.data_dir / 'proj', store.data_dir):
        with pytest.raises(ValueError, match='inside the data directory'):
            store.add_project('demo', inside)
        with pytest.raises(ValueError, match='inside the data directory'):
            store.add_project_root(project['id'], inside)
    assert [row['name'] for row in store.list_projects()] == ['safe']


def test_the_data_directory_inside_the_project_folder_stays_allowed(tmp_path):
    store = Store(tmp_path / 'repo' / 'data')
    assert store.add_project('demo', tmp_path / 'repo')


def test_the_refusal_is_a_fixed_sentence_with_a_polish_form(tmp_path):
    from cairnmind.terminal.terminal_i18n import tr
    store = Store(tmp_path / 'data')
    with pytest.raises(ValueError) as refusal:
        store.add_project('demo', store.data_dir / 'proj')
    assert tr(str(refusal.value), 'pl') != str(refusal.value)


def test_a_project_registered_inside_the_data_directory_by_an_earlier_version_is_named_by_indexing(tmp_path):
    from cairnmind.scip.ingest import index_project
    from cairnmind.storage.sql import insert_document
    store = Store(tmp_path / 'data')
    inside = store.data_dir / 'proj'
    inside.mkdir()
    (inside / 'a.py').write_text('x = 1\n')
    with store.transaction() as tx:  # as 0.12.0 registered it, before the refusal existed
        insert_document(tx, 'projects', {'id': 'old', 'name': 'old', 'root': str(inside), 'created_at': 'x'})
    result = index_project(store, 'old')
    assert result['files'] == 0
    assert any('inside the data directory' in error and "'old'" in error and 'project add' in error
               for error in result['errors'])
    assert [row['name'] for row in store.list_projects()] == ['old']  # nothing is removed
    assert any(str(inside.resolve()) in error and '`cairnmind project remove old --yes`' in error
               and '`cairnmind project add old NEWFOLDER`' in error for error in result['errors'])
    from cairnmind.memory import project_removal  # the advice works: the registration goes, its memory stays (#327)
    plan = project_removal.plan(store.settings, store, store.resolve_project('old'))
    said = ' '.join(project_removal.sentences(plan))  # the dry run promises no way back that the code refuses
    assert 'cairnmind project add cannot bring old back' in said and 'brings back the same project' not in said
    project_removal.apply(store, plan)
    assert store.list_projects() == [] and store.query("SELECT FROM projects WHERE id='old'")
    with pytest.raises(ValueError, match='inside the data directory'):
        store.add_project('old', inside)  # as predicted: the memory can never be attached to this folder again
    with pytest.raises(ValueError) as named:
        store.resolve_project('old')
    assert 'cannot bring it back' in str(named.value) and 'More, then Delete project memory' in str(named.value)
    assert 'forget' not in str(named.value)  # the command line no longer deletes (#347)
    listed = ' '.join(project_removal.removed_sentences(project_removal.removed_view(store)))
    assert 'cairnmind project add cannot bring old back' in listed


def test_an_extra_root_inside_the_data_directory_is_named_with_the_command_for_roots(tmp_path):
    from cairnmind.scip.ingest import index_project
    from cairnmind.storage.sql import insert_document
    store = Store(tmp_path / 'data')
    main = tmp_path / 'main'
    main.mkdir()
    (main / 'a.py').write_text('x = 1\n')
    project = store.add_project('p', main)
    inside = store.data_dir / 'extra'
    inside.mkdir()
    with store.transaction() as tx:
        insert_document(tx, 'project_roots', {'id': 'r1', 'project_id': project['id'], 'path': str(inside)})
    result = index_project(store, 'p')
    assert result['files'] == 1
    assert any(str(inside.resolve()) in error and '`cairnmind project root p NEWFOLDER`' in error
               and f'`cairnmind project remove p --root {inside.resolve()} --yes`' in error
               for error in result['errors'])


def test_the_credential_store_refusal_has_a_polish_form(tmp_path):
    from cairnmind.terminal.terminal_i18n import tr
    store = Store(tmp_path / 'data')
    (store.data_dir / 'credentials').mkdir()
    (store.data_dir / 'credentials' / 'api-keys.json').write_text('{}')
    with pytest.raises(ValueError) as refusal:
        store.add_project('demo', tmp_path)
    assert 'credential store' in str(refusal.value)
    assert tr(str(refusal.value), 'pl') != str(refusal.value)

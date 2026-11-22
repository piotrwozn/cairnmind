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
    from cairnmind.terminal_i18n import tr
    store = Store(tmp_path / 'data')
    with pytest.raises(ValueError) as refusal:
        store.add_project('demo', store.data_dir / 'proj')
    assert tr(str(refusal.value), 'pl') != str(refusal.value)


def test_a_project_registered_inside_the_data_directory_by_an_earlier_version_is_named_by_indexing(tmp_path):
    from cairnmind.ingest import index_project
    from cairnmind.sql import insert_document
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
    assert any(str(inside.resolve()) in error and 'cairnmind project add`' in error for error in result['errors'])


def test_an_extra_root_inside_the_data_directory_is_named_with_the_command_for_roots(tmp_path):
    from cairnmind.ingest import index_project
    from cairnmind.sql import insert_document
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
    assert any(str(inside.resolve()) in error and 'project root`' in error for error in result['errors'])


def test_the_credential_store_refusal_has_a_polish_form(tmp_path):
    from cairnmind.terminal_i18n import tr
    store = Store(tmp_path / 'data')
    (store.data_dir / 'credentials').mkdir()
    (store.data_dir / 'credentials' / 'api-keys.json').write_text('{}')
    with pytest.raises(ValueError) as refusal:
        store.add_project('demo', tmp_path)
    assert 'credential store' in str(refusal.value)
    assert tr(str(refusal.value), 'pl') != str(refusal.value)

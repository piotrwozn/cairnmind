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

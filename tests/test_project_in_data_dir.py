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


def test_the_data_directory_inside_the_project_folder_stays_allowed_and_is_left_out(tmp_path):
    store = Store(tmp_path / 'repo' / 'data')
    assert store.add_project('demo', tmp_path / 'repo')

"""Config concurrency regressions; all config and process state is temporary."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from src.infrastructure.config.env_manager import EnvManager


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    import socket
    def denied(*args, **kwargs):
        raise AssertionError('real network forbidden')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)


def test_every_public_writer_shares_cross_process_transaction(tmp_path):
    store = EnvManager(str(tmp_path / '.env'))
    assert store.update_values({'KEEP': 'fixture-a', 'DROP': 'fixture-old'})
    script = '''
import sys
from pathlib import Path
Path(sys.argv[2]).touch()
from src.infrastructure.config.env_manager import EnvManager
store = EnvManager(sys.argv[1])
assert store.apply_changes({'ROTATED': 'fixture-new'}, ['DROP'])
assert store.set_value('EXTRA', 'fixture-extra')
assert store.delete_keys(['TRANSIENT'])
'''
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    ready = tmp_path / 'ready'
    proc = None
    try:
        with store.transaction():
            proc = subprocess.Popen([sys.executable, '-c', script, str(store.env_file), str(ready)],
                                    cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready.exists()
            time.sleep(0.1)
            assert proc.poll() is None, 'subprocess must not pass the shared flock'
            assert store.update_values({'KEEP': 'fixture-b', 'TRANSIENT': 'fixture-temp'})
        output, error = proc.communicate(timeout=5)
        assert proc.returncode == 0, (output, error)
    finally:
        if proc and proc.poll() is None:
            proc.kill()
            proc.wait()
    assert store.read_env() == {'KEEP': 'fixture-b', 'ROTATED': 'fixture-new', 'EXTRA': 'fixture-extra'}


def test_atomic_replace_failure_keeps_original_config(tmp_path, monkeypatch):
    store = EnvManager(str(tmp_path / '.env'))
    assert store.update_values({'PAIR_KEY': 'fixture-a', 'PAIR_URL': 'https://a.invalid'})
    from src.infrastructure.config import env_manager as module
    def fail(*args):
        raise OSError('fixture-only')
    monkeypatch.setattr(module.os, 'replace', fail)
    assert not store.update_values({'PAIR_KEY': 'fixture-b', 'PAIR_URL': 'https://b.invalid'})
    assert store.read_env() == {'PAIR_KEY': 'fixture-a', 'PAIR_URL': 'https://a.invalid'}
    assert not list(tmp_path.glob('.env-write-*'))


def test_snapshot_is_immutable_and_does_not_follow_updates(tmp_path):
    store = EnvManager(str(tmp_path / '.env'))
    store.set_value('PAIR_KEY', 'fixture-a')
    snap = store.snapshot()
    store.set_value('PAIR_KEY', 'fixture-b')
    assert snap.get_value('PAIR_KEY') == 'fixture-a'
    with pytest.raises(TypeError):
        snap.values['PAIR_KEY'] = 'fixture-evil'


def test_notification_loader_cannot_tear_target_and_token(tmp_path, monkeypatch):
    from src.services import notification_config_service as config
    store = EnvManager(str(tmp_path / '.env'))
    store.update_values({'GOTIFY_URL': 'https://old.invalid', 'GOTIFY_TOKEN': 'fixture-old'})
    monkeypatch.setattr(config, 'env_manager', store)
    original = store.get_value
    switched = []
    def interleave(name, default=None):
        value = original(name, default)
        if name == 'GOTIFY_URL' and not switched:
            switched.append(True)
            store.update_values({'GOTIFY_URL': 'https://new.invalid', 'GOTIFY_TOKEN': 'fixture-new'})
        return value
    monkeypatch.setattr(store, 'get_value', interleave)
    result = config.load_notification_settings()
    assert (result.gotify_url, result.gotify_token) in {
        ('https://old.invalid', 'fixture-old'), ('https://new.invalid', 'fixture-new')}


def test_notification_probe_freezes_all_configuration_before_send(tmp_path, monkeypatch):
    from src.mcp.admin_tools import StoredNotificationProbe
    from src.services import notification_service as service
    store = EnvManager(str(tmp_path / '.env'))
    store.update_values({'GOTIFY_URL': 'https://old.invalid', 'GOTIFY_TOKEN': 'fixture-old'})
    captured = []
    class Fake:
        async def send_test_notification(self):
            return {'gotify': {'success': True, 'attempted': True, 'outcome': 'accepted'}}
    def factory(settings):
        store.update_values({'GOTIFY_URL': 'https://new.invalid', 'GOTIFY_TOKEN': 'fixture-new'})
        captured.append((settings.gotify_url, settings.gotify_token))
        return Fake()
    monkeypatch.setattr(service, 'build_notification_service', factory)
    assert asyncio.run(StoredNotificationProbe().run(store, channel='gotify'))['success']
    assert captured == [('https://old.invalid', 'fixture-old')]

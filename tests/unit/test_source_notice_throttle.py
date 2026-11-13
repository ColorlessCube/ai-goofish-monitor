"""Characterization and concurrency checks for durable source notice reservations."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3

from src.services.source_notice_throttle import reserve_source_notice


def test_reservation_survives_new_calls_until_hour_expires(tmp_path):
    path = tmp_path / 'notices.sqlite3'
    assert reserve_source_notice(path, 'fixture', now=1000)
    assert not reserve_source_notice(path, 'fixture', now=4599)
    assert reserve_source_notice(path, 'another-task', now=4599)
    assert reserve_source_notice(path, 'fixture', now=4600)
    assert path.stat().st_mode & 0o777 == 0o600
    assert b'fixture' not in path.read_bytes()


def test_concurrent_reservations_submit_at_most_once(tmp_path):
    path = tmp_path / 'notices.sqlite3'
    with ThreadPoolExecutor(max_workers=8) as workers:
        outcomes = list(workers.map(lambda _: reserve_source_notice(path, 'fixture', now=1000), range(16)))
    assert outcomes.count(True) == 1


def test_capacity_is_bounded_and_expires_without_eviction_of_recent_entries(tmp_path):
    path = tmp_path / 'notices.sqlite3'
    for number in range(128):
        assert reserve_source_notice(path, str(number), now=1000)
    assert not reserve_source_notice(path, 'new-task', now=1001)
    assert not reserve_source_notice(path, '0', now=1001)
    assert reserve_source_notice(path, 'new-task', now=4600)
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 1


def test_corrupt_or_unavailable_state_fails_closed(tmp_path):
    path = tmp_path / 'notices.sqlite3'
    original = b'not a database'
    path.write_bytes(original)
    assert not reserve_source_notice(path, 'fixture', now=1000)
    assert path.read_bytes() == original
    assert not reserve_source_notice(tmp_path, 'fixture', now=1000)


def test_symlink_is_not_followed(tmp_path):
    target = tmp_path / 'untouched'
    target.write_bytes(b'untouched')
    path = tmp_path / 'notices.sqlite3'
    path.symlink_to(target)
    assert not reserve_source_notice(path, 'fixture', now=1000)
    assert target.read_bytes() == b'untouched'

"""Batched normalized technical facts shared by file records and revision history."""
import json
import sqlite3

from app.models.inventory import FileObservation

OBSERVATION_FIELDS = ('root_id', 'relative_path', 'media_id', 'scope', 'filesystem_id', 'inode',
                      'generation', 'size', 'mtime_ns', 'ctime_ns', 'hardlinks')
STREAM_FIELDS = ('kind', 'codec', 'language', 'title', 'bitrate', 'width', 'height', 'resolution_class',
                 'scan_type', 'field_order', 'frame_rate', 'pixel_format', 'channels', 'channel_layout', 'sample_rate')


def store_observation(connection: sqlite3.Connection, key: str, observation: FileObservation) -> None:
    connection.execute('INSERT INTO media_items VALUES (?,?) ON CONFLICT DO NOTHING',
                       (observation.media_id, observation.scope))
    scope = connection.execute('SELECT scope FROM media_items WHERE media_id=?', (observation.media_id,)).fetchone()[0]
    if scope != observation.scope:
        raise ValueError('A semantic media identity cannot change scope.')
    facts = observation.model_dump(mode='json')
    probe = observation.probe
    fields = ['observation_id', *OBSERVATION_FIELDS, 'fingerprints', 'container', 'container_bitrate', 'duration_seconds', 'probe_metadata']
    values = [key, *(facts[field] for field in OBSERVATION_FIELDS), json.dumps(facts['fingerprints']),
              probe.container if probe else None, probe.container_bitrate if probe else None,
              probe.duration_seconds if probe else None, json.dumps(probe.metadata) if probe else None]
    connection.execute(f"INSERT INTO observations ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)}) "
                       + 'ON CONFLICT(observation_id) DO UPDATE SET '
                       + ','.join(f'{f}=excluded.{f}' for f in fields[1:]), values)
    connection.execute('DELETE FROM streams WHERE observation_id=?', (key,))
    connection.execute('DELETE FROM chapters WHERE observation_id=?', (key,))
    if probe is None:
        return
    for stream in probe.streams:
        data = stream.model_dump(mode='json')
        fields = ['observation_id', 'stream_index', *STREAM_FIELDS, 'dispositions', 'hdr', 'metadata', 'bit_depth', 'color_primaries', 'color_transfer', 'color_matrix']
        values = [key, stream.index, *(data[f] for f in STREAM_FIELDS), json.dumps(data['dispositions']),
                  json.dumps(data['hdr']) if stream.hdr else None, json.dumps(data['metadata']),
                  stream.hdr.bit_depth if stream.hdr else None, stream.hdr.primaries if stream.hdr else None,
                  stream.hdr.transfer if stream.hdr else None, stream.hdr.matrix if stream.hdr else None]
        connection.execute(f"INSERT INTO streams ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", values)
    connection.executemany('INSERT INTO chapters VALUES (?,?,?)',
                           [(key, i, chapter.model_dump_json()) for i, chapter in enumerate(probe.chapters)])


def observations(connection: sqlite3.Connection, keys: list[str]) -> dict[str, FileObservation]:
    result = {}
    for start in range(0, len(keys), 500):
        batch = keys[start:start + 500]
        marks = ','.join('?' for _ in batch)
        rows = connection.execute(f'SELECT * FROM observations WHERE observation_id IN ({marks})', batch).fetchall()
        facts = {}
        for row in rows:
            data = dict(row)
            key = data.pop('observation_id')
            data['fingerprints'] = json.loads(data['fingerprints'])
            container = data.pop('container')
            header = {'container': container, 'container_bitrate': data.pop('container_bitrate'),
                      'duration_seconds': data.pop('duration_seconds'), 'metadata': json.loads(data.pop('probe_metadata') or '{}')}
            data['probe'] = header if container is not None else None
            if data['probe'] is not None:
                data['probe'].update(streams=[], chapters=[])
            facts[key] = data
        for row in connection.execute(f'SELECT * FROM streams WHERE observation_id IN ({marks}) ORDER BY stream_index', batch):
            data = dict(row)
            key = data.pop('observation_id')
            data['index'] = data.pop('stream_index')
            for column in ('bit_depth', 'color_primaries', 'color_transfer', 'color_matrix'):
                data.pop(column)
            for field in ('dispositions', 'hdr', 'metadata'):
                data[field] = json.loads(data[field]) if data[field] is not None else None
            facts[key]['probe']['streams'].append(data)
        for row in connection.execute(f'SELECT * FROM chapters WHERE observation_id IN ({marks}) ORDER BY ordinal', batch):
            facts[row['observation_id']]['probe']['chapters'].append(json.loads(row['payload']))
        result.update({key: FileObservation.model_validate(data) for key, data in facts.items()})
    return result

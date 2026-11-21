"""事件级正负偏差、净结算与定稿幂等/叠加。"""
import re
from copy import deepcopy
from decimal import Decimal

import pytest

from app import store
from app.engine import evaluate
from app.config import ROOT
from app.settlement import finalization_digest, finalization_payload


def _set_power(csv_text, sample_ids, power):
    for sample_id in sample_ids:
        csv_text = re.sub(rf'({sample_id},TELEMETRY,[^,]+,[^,]+,)60,', rf'\g<1>{power},', csv_text)
    return csv_text


@pytest.fixture
def mixed():
    """同一 INV-A 事件内前段超发、后段欠发。"""
    text = (ROOT / 'sample_data/clean.csv').read_text()
    return evaluate({'csv_text': _set_power(text, ['INV-A-00'], '110')})


@pytest.fixture
def all_surplus():
    """整段事件都高于基线：净结算为0，但超发单列不抹成负损失。"""
    text = (ROOT / 'sample_data/clean.csv').read_text()
    return evaluate({'csv_text': _set_power(text, ['INV-A-00', 'INV-A-10', 'INV-A-20'], '110')})


def test_clean_event_settles_gross(clean):
    a = next(e for e in clean['event_settlements'] if e['device'] == 'INV-A')
    assert a['status'] == 'SETTLED'
    assert a['positive_loss_kwh'] == '20.0000'
    assert a['negative_deviation_kwh'] == '0.0000'
    assert a['net_settlement_kwh'] == '20.0000'
    assert a['unoffset_surplus_kwh'] == '0.0000'


def test_negative_deviation_offsets_same_event(mixed):
    a = next(e for e in mixed['event_settlements'] if e['device'] == 'INV-A')
    # 前10分钟实际110>基线100，超发1.6667kWh；后20分钟欠发13.3333kWh。
    assert a['positive_loss_kwh'] == '13.3333'
    assert a['negative_deviation_kwh'] == '1.6666'
    assert a['signed_deviation_kwh'] == '11.6667'
    assert a['net_settlement_kwh'] == '11.6667'
    assert a['unoffset_surplus_kwh'] == '0.0000'
    # 正负偏差都留在偏差来源里，不被片段内抹平。
    kinds = {s['source_kind'] for s in mixed['deviation_sources'] if s['device'] == 'INV-A'}
    assert kinds == {'正向损失', '负向偏差(超发)'}


def test_event_identities_close_after_quantization(mixed):
    for event in mixed['event_settlements']:
        baseline, actual = Decimal(event['baseline_kwh']), Decimal(event['actual_kwh'])
        positive, negative = Decimal(event['positive_loss_kwh']), Decimal(event['negative_deviation_kwh'])
        net, unoffset = Decimal(event['net_settlement_kwh']), Decimal(event['unoffset_surplus_kwh'])
        assert baseline - actual == net - unoffset
        assert positive - negative == net - unoffset
        assert event['balanced'] is True


def test_surplus_never_negative_settlement(all_surplus):
    a = next(e for e in all_surplus['event_settlements'] if e['device'] == 'INV-A')
    assert a['positive_loss_kwh'] == '0.0000'
    assert a['net_settlement_kwh'] == '0.0000'
    assert a['unoffset_surplus_kwh'] == '5.0000'
    assert a['signed_deviation_kwh'] == '-5.0000'
    assert a['code'] == 'SETTLED_SURPLUS'
    assert a['balanced'] is True


def test_negative_deviation_does_not_cross_events(all_surplus):
    b = next(e for e in all_surplus['event_settlements'] if e['device'] == 'INV-B')
    assert b['net_settlement_kwh'] == '6.6667'
    assert all_surplus['settlement_balance']['unoffset_surplus_kwh'] == '5.0000'
    # 事件间不结转：总净结算仍是 B 的 6.6667，A 的超发不冲抵 B。
    assert all_surplus['settlement_balance']['net_settlement_kwh'] == '6.6667'


def test_pending_event_blocks_whole_event(sample):
    a = next(e for e in sample['event_settlements'] if e['device'] == 'INV-A')
    assert a['status'] == 'PENDING'
    assert a['net_settlement_kwh'] is None
    assert a['unknown_segments']
    # 已定事件仍可结算，未定事件不被部分冲抵后少算。
    b = next(e for e in sample['event_settlements'] if e['device'] == 'INV-B')
    assert b['status'] == 'SETTLED'


def test_archive_blocked_by_pending_event(sample):
    assert sample['archive']['status'] == '待复核'
    assert sample['archive']['pending_events'] == 1


def test_archive_bundle_contains_settlement(clean):
    from app.archive import serialize
    import io, json, zipfile
    bundle = zipfile.ZipFile(io.BytesIO(serialize(clean)))
    settlement = json.loads(bundle.read('settlement.json'))
    assert settlement['balance']['net_settlement_kwh'] == '26.6667'
    assert len(settlement['events']) == 2
    assert any(s['source_kind'] == '正向损失' for s in settlement['sources'])


def test_finalize_is_idempotent(tmp_path, clean):
    path = str(tmp_path / 'f.sqlite')
    run_id = clean['manifest']['run_id']
    first = store.finalize(path, run_id, clean['event_settlements'])
    assert len(first['created']) == 2
    second = store.finalize(path, run_id, clean['event_settlements'])
    assert second['created'] == []
    assert len(second['reused']) == 2
    assert len(store.finalizations(path)) == 2


def test_retry_via_rerun_keeps_one_finalization(tmp_path, clean):
    path = str(tmp_path / 'f.sqlite')
    run_id = clean['manifest']['run_id']
    store.finalize(path, run_id, clean['event_settlements'])
    again = evaluate({'dataset': 'clean'})
    assert again['manifest']['run_id'] == run_id
    result = store.finalize(path, run_id, again['event_settlements'])
    assert result['created'] == [] and len(store.finalizations(path)) == 2


def test_changed_result_supersedes_but_keeps_old(tmp_path, clean, mixed):
    path = str(tmp_path / 'f.sqlite')
    store.finalize(path, clean['manifest']['run_id'], clean['event_settlements'])
    result = store.finalize(path, mixed['manifest']['run_id'], mixed['event_settlements'])
    assert result['created']
    a_versions = [f for f in store.finalizations(path) if f['device'] == 'INV-A']
    assert [f['version'] for f in a_versions] == [2, 1]
    assert a_versions[0]['finalization_status'] == '已定稿'
    assert a_versions[1]['finalization_status'] == '已替换'
    assert a_versions[0]['supersedes'] == a_versions[1]['finalization_id']
    # 旧版完整保留，可复核当时采用的基线与净结算。
    assert a_versions[1]['positive_loss_kwh'] == '20.0000'


def test_failed_switch_keeps_finalizations(tmp_path, clean, sample):
    """切换样例后目标事件未定时，既有定稿不被删除或改写。"""
    path = str(tmp_path / 'f.sqlite')
    store.finalize(path, clean['manifest']['run_id'], clean['event_settlements'])
    result = store.finalize(path, sample['manifest']['run_id'], sample['event_settlements'])
    assert result['created'] == []
    assert any(key.startswith('INV-A~') for key in result['pending_events'])
    records = store.finalizations(path, 'INV-A~1792029600~1792031400')
    assert len(records) == 1
    assert records[0]['finalization_status'] == '已定稿'
    assert records[0]['net_settlement_kwh'] == '20.0000'


def test_pending_event_not_finalized(tmp_path, sample):
    path = str(tmp_path / 'f.sqlite')
    result = store.finalize(path, sample['manifest']['run_id'], sample['event_settlements'])
    assert {f['device'] for f in result['created']} == {'INV-B'}
    assert result['pending_events']


def test_finalization_digest_ignores_source_run(clean):
    first = finalization_payload('run-a', clean['event_settlements'][0])
    second = finalization_payload('run-b', clean['event_settlements'][0])
    assert finalization_digest(first) == finalization_digest(second)
    changed = deepcopy(first)
    changed['net_settlement_kwh'] = '999.0000'
    assert finalization_digest(changed) != finalization_digest(first)


def test_rerun_of_dirty_event_after_fix_upgrades_not_duplicates(tmp_path):
    """同一事件设备与并集不变，补齐对照后重试只新增版本，不产生第二条事件。"""
    path = str(tmp_path / 'f.sqlite')
    dirty = evaluate({'dataset': 'dirty'})
    first = store.finalize(path, dirty['manifest']['run_id'], dirty['event_settlements'])
    assert {f['device'] for f in first['created']} == {'INV-B'}
    clean = evaluate({'dataset': 'clean'})
    result = store.finalize(path, clean['manifest']['run_id'], clean['event_settlements'])
    a_versions = [f for f in store.finalizations(path) if f['device'] == 'INV-A']
    assert [f['version'] for f in a_versions] == [1]
    assert {f['device'] for f in result['created']} == {'INV-A'}
    assert {f['device'] for f in result['reused']} == {'INV-B'}
    keys = {f['event_key'] for f in store.finalizations(path)}
    assert keys == {
        'INV-A~1792029600~1792031400',
        'INV-B~1792029600~1792030800',
    }


def test_api_finalize_and_retry(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'api.sqlite'))
    client = TestClient(main.app)
    run = client.post('/api/runs', json={'dataset': 'clean'}).json()
    run_id = run['manifest']['run_id']
    assert run['finalizations'] == []
    first = client.post(f'/api/runs/{run_id}/finalize').json()
    assert len(first['created']) == 2 and first['reused'] == []
    again = client.post(f'/api/runs/{run_id}/finalize').json()
    assert again['created'] == [] and len(again['reused']) == 2
    assert len(client.get('/api/finalizations').json()) == 2


def test_api_single_event_finalization(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'api.sqlite'))
    client = TestClient(main.app)
    run = client.post('/api/runs', json={'dataset': 'clean'}).json()
    run_id = run['manifest']['run_id']
    key = next(e for e in run['event_settlements'] if e['device'] == 'INV-A')['event_key']
    result = client.post('/api/finalizations', json={'run_id': run_id, 'event_key': key}).json()
    assert len(result['created']) == 1
    history = client.get('/api/finalizations', params={'event_key': key}).json()
    assert len(history) == 1 and history[0]['device'] == 'INV-A'
    assert client.post('/api/finalizations', json={'run_id': run_id, 'event_key': 'nope'}).status_code == 404


def test_api_pending_event_not_finalized(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'api.sqlite'))
    client = TestClient(main.app)
    run = client.post('/api/runs', json={'dataset': 'dirty'}).json()
    run_id = run['manifest']['run_id']
    result = client.post(f'/api/runs/{run_id}/finalize').json()
    assert {f['device'] for f in result['created']} == {'INV-B'}
    assert result['pending_events']
    # 未定事件在页面上标为“不生成定稿”，已确认的 INV-B 定稿仍在。
    html = client.get('/reconcile?run_id=' + run_id).text
    assert '未定，不生成定稿' in html
    assert '已定稿' in html


def test_api_finalization_survives_sample_switch(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'api.sqlite'))
    client = TestClient(main.app)
    clean_id = client.post('/api/runs', json={'dataset': 'clean'}).json()['manifest']['run_id']
    client.post(f'/api/runs/{clean_id}/finalize')
    dirty_id = client.post('/api/runs', json={'dataset': 'dirty'}).json()['manifest']['run_id']
    client.post(f'/api/runs/{dirty_id}/finalize')
    history = client.get('/api/finalizations').json()
    assert len(history) == 2
    assert all(f['finalization_status'] == '已定稿' for f in history)
    exported = client.get('/api/export/' + dirty_id)
    assert exported.status_code == 200


def test_digest_ignores_provenance_ids_but_tracks_numbers(clean):
    """重导入后遥测/对照/事件编号变化、结果相同：复用定稿而非新增版本。"""
    first = finalization_payload('run-a', clean['event_settlements'][0])
    renumbered = deepcopy(first)
    renumbered.update(source_run='run-b', event_ids=['EA1-REIMPORT'], reference_ids=['REF-A1-Z', 'REF-A2-Z'])
    assert finalization_digest(renumbered) == finalization_digest(first)
    assert finalization_digest({**first, 'baseline_kwh': '99.0000'}) != finalization_digest(first)


def test_reimport_with_same_numbers_reuses_version(tmp_path, clean, monkeypatch):
    path = str(tmp_path / 'f.sqlite')
    store.finalize(path, clean['manifest']['run_id'], clean['event_settlements'])
    renumbered = deepcopy(clean['event_settlements'])
    for event in renumbered:
        event['event_ids'] = [f'{e}-REIMPORT' for e in event['event_ids']]
        event['reference_ids'] = [f'{r}-REIMPORT' for r in event['reference_ids']]
    result = store.finalize(path, 'another-run', renumbered)
    assert result['created'] == [] and len(result['reused']) == 2
    assert len(store.finalizations(path)) == 2


def test_legacy_stored_run_backfilled_on_read(tmp_path, clean):
    """规则升级前保存的批次缺事件结算字段，读取时按当前口径补算。"""
    import json
    path = str(tmp_path / 'legacy.sqlite')
    run_id = clean['manifest']['run_id']
    legacy = {k: v for k, v in clean.items()
              if k not in {'event_settlements', 'deviation_sources', 'settlement_balance'}}
    with store.connect(path) as con:
        con.execute('INSERT INTO runs(id,digest,result) VALUES(?,?,?)',
                    (run_id, legacy['manifest']['sha256'], json.dumps(legacy, ensure_ascii=False)))
    loaded = store.get(path, run_id)
    assert [e['device'] for e in loaded['event_settlements']] == ['INV-A', 'INV-B']
    assert loaded['settlement_balance']['net_settlement_kwh'] == '26.6667'
    assert loaded['deviation_sources']

    # 旧批可直接定稿，且与新版重算结果视为同一结果，不重复生成。
    first = store.finalize(path, run_id, loaded['event_settlements'])
    assert len(first['created']) == 2
    again = store.finalize(path, run_id, clean['event_settlements'])
    assert again['created'] == []

"""事件级正负偏差分解与净结算。

裁定口径：

* 片段级分别保留正向损失 max(基线-实际,0) 与负向偏差 max(实际-基线,0)，
  任何一方都不在片段内被抹平。
* 事件级 net = 正向损失 - 负向偏差，但净结算不低于 0；超发多于损失的部分
  记为“未冲抵超发”，既不跨事件结转，也不计成负损失。
* 负向偏差只冲抵同一事件、同一设备、同一并集区间内的正向损失。
* 事件只要含未定片段（缺测、无合格对照、目标故障），整段不生成净结算，
  避免用部分已知片段冲抵后把受限时段算少。
* 恒等式：基线电量 - 实际电量 = 净结算电量 - 未冲抵超发电量。
"""

import hashlib
import json
from collections import defaultdict
from decimal import Decimal

from app.coerce import number
from app.config import RULE_VERSION

P4 = Decimal('.0001')
ZERO = Decimal(0)


def quant(value):
    return format(Decimal(value).quantize(P4), 'f')


def _sources(rows):
    sources = []
    for row in rows:
        baseline, actual = number(row['baseline_kw']), number(row['actual_kw'])
        hours = Decimal(row['seconds']) / 3600
        signed = (number(row['deviation_kwh']) if row.get('deviation_kwh') is not None
                  else (baseline - actual) * hours)
        if signed == 0:
            continue
        surplus = (number(row['surplus_kwh']) if row.get('surplus_kwh') is not None
                   else max(actual - baseline, ZERO) * hours)
        sources.append({
            'event_key': row['event_key'],
            'device': row['device'],
            'start': row['start'],
            'end': row['end'],
            'seconds': row['seconds'],
            'sample_id': row.get('sample_id', ''),
            'baseline_kw': row['baseline_kw'],
            'actual_kw': row['actual_kw'],
            'deviation_kwh': quant(signed),
            'surplus_kwh': quant(surplus),
            'source_kind': '正向损失' if signed > 0 else '负向偏差(超发)',
            'reference_ids': list(row.get('reference_ids', [])),
        })
    return sources


def _aggregate(rows):
    def total(key):
        return sum((number(r[key]) for r in rows), ZERO)
    # 兼容旧批结果：只含 loss_kwh / actual_kwh 时按同口径现算负向偏差。
    # 先在未量化精度上求各分量，再按定稿精度(0.0001kWh)闭合展示值。
    # 展示值由恒等式反推：负向偏差采用实际冲抵额(不超过正向损失)，
    # 净结算与未冲抵超发由量化后的净偏差拆分，避免各列独立四舍五入后
    # 出现“基线−实际 ≠ 净结算−未冲抵超发”的假不平。
    baseline, actual = quant(total('baseline_kwh')), quant(total('actual_kwh'))
    gross_exact = total('loss_kwh')
    signed = number(baseline) - number(actual)
    net, unoffset = max(signed, ZERO), max(-signed, ZERO)
    positive = number(quant(gross_exact))
    # 负向偏差取“正向损失 − 净偏差”，保证 正向损失 − 负向偏差 = 净偏差；
    # 与逐段四舍五入的原始超发最多差 0.0001kWh，且不改变净结算。
    negative = max(positive - signed, ZERO)
    return {
        'baseline_kwh': baseline,
        'actual_kwh': actual,
        'positive_loss_kwh': quant(positive),
        'negative_deviation_kwh': quant(negative),
        'signed_deviation_kwh': quant(signed),
        'net_settlement_kwh': quant(net),
        'unoffset_surplus_kwh': quant(unoffset),
        # 两条恒等式在展示精度上必须闭合；否则该事件不得定稿。
        'balanced': quant(number(baseline) - number(actual)) == quant(net - unoffset)
        and quant(positive - negative) == quant(signed),
    }


def _pending(union, rows):
    unknown = [{
        'start': r['start'], 'end': r['end'], 'seconds': r['seconds'],
        'sample_id': r.get('sample_id', ''), 'code': r.get('code', 'UNKNOWN'),
    } for r in rows if r.get('loss_kwh') is None or r.get('baseline_kw') is None or r.get('actual_kw') is None]
    return {
        'event_key': union['event_key'], 'device': union['device'],
        'start': union['start'], 'end': union['end'], 'seconds': union['seconds'],
        'event_ids': list(union['event_ids']), 'segment_count': len(rows),
        'baseline_method': f'同区域同采样时刻同辐照档参考中位数×目标额定 / {RULE_VERSION}',
        'status': 'PENDING', 'code': (unknown[0]['code'] if unknown else 'NO_SEGMENT'),
        'baseline_kwh': None, 'actual_kwh': None, 'positive_loss_kwh': None,
        'negative_deviation_kwh': None, 'signed_deviation_kwh': None,
        'net_settlement_kwh': None, 'unoffset_surplus_kwh': None,
        'reference_ids': [], 'unknown_segments': unknown, 'balanced': False,
    }


def build(state):
    grouped = defaultdict(list)
    for row in state['energy_rows']:
        grouped[row['event_key']].append(row)
    settlements, sources = [], []
    for union in state['event_unions']:
        rows = sorted(grouped.get(union['event_key'], []), key=lambda r: (r['start'], r['end']))
        if not rows or any(r.get('loss_kwh') is None or r.get('baseline_kw') is None or r.get('actual_kw') is None for r in rows):
            settlements.append(_pending(union, rows))
            continue
        aggregate = _aggregate(rows)
        references = sorted({ref for row in rows for ref in row.get('reference_ids', [])})
        settlements.append({
            'event_key': union['event_key'], 'device': union['device'],
            'start': union['start'], 'end': union['end'], 'seconds': union['seconds'],
            'event_ids': list(union['event_ids']), 'segment_count': len(rows),
            'baseline_method': f'同区域同采样时刻同辐照档参考中位数×目标额定 / {RULE_VERSION}',
            'status': 'SETTLED',
            'code': 'SETTLED' if number(aggregate['net_settlement_kwh']) > 0 else
                    ('SETTLED_SURPLUS' if number(aggregate['unoffset_surplus_kwh']) > 0 else 'SETTLED_ZERO'),
            'reference_ids': references, 'unknown_segments': [],
            **aggregate,
        })
        sources.extend(_sources(rows))
    settled = [s for s in settlements if s['status'] == 'SETTLED']
    summary = {
        'events': len(settlements), 'settled_events': len(settled),
        'pending_events': len(settlements) - len(settled),
    }
    if settled:
        summary.update({k: quant(sum((number(s[k]) for s in settled), ZERO)) for k in
                        ['baseline_kwh', 'actual_kwh', 'positive_loss_kwh', 'negative_deviation_kwh',
                         'net_settlement_kwh', 'unoffset_surplus_kwh', 'signed_deviation_kwh']})
    else:
        summary.update({k: None for k in
                        ['baseline_kwh', 'actual_kwh', 'positive_loss_kwh', 'negative_deviation_kwh',
                         'net_settlement_kwh', 'unoffset_surplus_kwh', 'signed_deviation_kwh']})
    summary['balanced'] = bool(settled) and all(s['balanced'] for s in settled) and (
        quant(number(summary['baseline_kwh']) - number(summary['actual_kwh']))
        == quant(number(summary['net_settlement_kwh']) - number(summary['unoffset_surplus_kwh'])))
    state['event_settlements'] = settlements
    state['deviation_sources'] = sources
    state['settlement_balance'] = summary
    return summary


def finalization_payload(run_id, settlement):
    return {
        'event_key': settlement['event_key'],
        'device': settlement['device'],
        'start': settlement['start'],
        'end': settlement['end'],
        'seconds': settlement['seconds'],
        'event_ids': list(settlement['event_ids']),
        'baseline_method': settlement['baseline_method'],
        'baseline_kwh': settlement['baseline_kwh'],
        'actual_kwh': settlement['actual_kwh'],
        'positive_loss_kwh': settlement['positive_loss_kwh'],
        'negative_deviation_kwh': settlement['negative_deviation_kwh'],
        'signed_deviation_kwh': settlement['signed_deviation_kwh'],
        'net_settlement_kwh': settlement['net_settlement_kwh'],
        'unoffset_surplus_kwh': settlement['unoffset_surplus_kwh'],
        'reference_ids': list(settlement['reference_ids']),
        'segment_count': settlement['segment_count'],
        'rule_version': RULE_VERSION,
        'source_run': run_id,
    }


def finalization_digest(payload):
    # 摘要只覆盖结算结果本身；来源批次、事件编号与遥测/对照编号属于证据出处，
    # 随版本保存供复核，但重导入后编号变化不应生成重复定稿。
    provenance = {'source_run', 'event_ids', 'reference_ids'}
    content = {k: v for k, v in payload.items() if k not in provenance}
    return hashlib.sha256(
        (json.dumps(content, ensure_ascii=False, sort_keys=True) + RULE_VERSION).encode()
    ).hexdigest()

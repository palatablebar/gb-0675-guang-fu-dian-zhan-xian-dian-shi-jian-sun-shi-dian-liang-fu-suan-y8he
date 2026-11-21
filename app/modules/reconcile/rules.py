from decimal import Decimal
from app.coerce import number
from app.settlement import build

def reconcile(state):
    known=[r for r in state['energy_rows'] if r['loss_kwh'] is not None];unknown=[r for r in state['energy_rows'] if r['loss_kwh'] is None]
    sums={k:sum((number(r[k]) for r in known),Decimal(0)) for k in ['loss_kwh','actual_kwh','baseline_kwh','surplus_kwh']}
    union_seconds=sum(r['seconds'] for r in state['event_unions']);slice_seconds=sum(r['seconds'] for r in state['energy_rows']);c=state['counts'];explained=len(state['records'])+len(state['quarantine'])+c['blank']+c['duplicate']+c['repeated_header']
    # Decimal周期小数允许最后一位精度误差，展示前统一量化到0.0001kWh再检查。
    quant=lambda v:v.quantize(Decimal('.0001'))
    # 能量恒等式：基线电量 − 实际电量 = 正向损失 − 负向偏差(超发)。超发不静默清零。
    deviation=sums['baseline_kwh']-sums['actual_kwh']
    return {'input':c['input'],'accepted':len(state['records']),'quarantined':len(state['quarantine']),'row_balanced':explained==c['input'],'union_seconds':union_seconds,'slice_seconds':slice_seconds,'time_balanced':union_seconds==slice_seconds,'loss_kwh':str(quant(sums['loss_kwh'])),'actual_kwh':str(quant(sums['actual_kwh'])),'baseline_kwh':str(quant(sums['baseline_kwh'])),'negative_kwh':str(quant(sums['surplus_kwh'])),'signed_deviation_kwh':str(quant(deviation)),'energy_balanced':quant(deviation)==quant(sums['loss_kwh']-sums['surplus_kwh']),'unknown_segments':len(unknown),'unknown_seconds':sum(r['seconds'] for r in unknown)}
def apply(state):
    state['balance']=reconcile(state)
    settlement=build(state)
    state['views']['reconcile']={'title':'时间与能量四账','rows':[state['balance']],'metrics':{'确定损失_kWh':state['balance']['loss_kwh'],'负向偏差_kWh':state['balance']['negative_kwh'],'未定分钟':state['balance']['unknown_seconds']//60,'并集分钟':state['balance']['union_seconds']//60}}

import hashlib,json

def archive_decision(state):
    b=state['balance']
    settlement=state.get('settlement_balance') or {}
    settled=settlement.get('settled_events',0)>0 and not settlement.get('pending_events')
    ready=(b['row_balanced'] and b['time_balanced'] and b['energy_balanced'] and not b['unknown_segments']
           and not state['quarantine'] and settled and settlement.get('balanced'))
    return {'status':'可归档' if ready else '待复核','seal':hashlib.sha256(json.dumps(state['energy_rows'],sort_keys=True).encode()).hexdigest(),'segments':len(state['energy_rows']),'unknown':b['unknown_segments'],'pending_events':settlement.get('pending_events',0),'settled_events':settlement.get('settled_events',0)}
def apply(state):
    state['archive']=archive_decision(state);state['views']['archive']={'title':'运行凭证','rows':state['event_settlements'],'metrics':{'切片':state['archive']['segments'],'未定':state['archive']['unknown'],'未定事件':state['archive']['pending_events'],'状态':state['archive']['status']}}

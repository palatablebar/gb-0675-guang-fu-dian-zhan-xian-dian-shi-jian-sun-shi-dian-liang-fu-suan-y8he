import io, csv, json, zipfile, hashlib
from app.errors import BusinessError

def serialize(result):
    if not result.get('manifest') or not result.get('views'):
        raise BusinessError('INCOMPLETE_RUN','批次尚未完成')
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json',json.dumps({**result['manifest'],'revision':result.get('revision',1),'status':result.get('status','待复核')},ensure_ascii=False,indent=2))
        archive.writestr('evidence.json',json.dumps({'counts':result['counts'],'views':result['views'],'quarantine':result['quarantine']},ensure_ascii=False,indent=2))
        # 事件定稿与偏差来源一并封存，使采用的基线、正负偏差和净结算在同包内互相解释。
        archive.writestr('settlement.json',json.dumps({'balance':result.get('settlement_balance'),'events':result.get('event_settlements',[]),'sources':result.get('deviation_sources',[])},ensure_ascii=False,indent=2))
        archive.writestr('finalizations.json',json.dumps(result.get('finalizations',[]),ensure_ascii=False,indent=2))
    return buffer.getvalue()

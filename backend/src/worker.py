import json,os,socket,time,logging
from redis import Redis
from .config.database import Session
from .config.settings import REDIS_URL,ARTIFACT_DIR
from .models.run import Run
from .models import source,record,user,note,audit,coverage
from .services.algorithm import calculate
from .services.audit import log
from .services.export import csv_response
from .services import coverage as coverage_service
queue=Redis.from_url(REDIS_URL)
ARTIFACT_DIR.mkdir(parents=True,exist_ok=True)
RESCAN_SECONDS=10
# 稳定的 worker 身份（容器名）：进程重启后仍能认领自己上次遗留的占用。
OWNER=socket.gethostname()
def _recover():
    try:
        with Session() as db:
            coverage_service.recover_running_items(db,OWNER)
            pending=coverage_service.incomplete_batches(db)
        for batch_id in pending:
            coverage_service.enqueue(batch_id)
        return pending
    except Exception:
        logging.exception('coverage recovery failed')
        return []
def _scan():
    try:
        with Session() as db:
            return coverage_service.incomplete_batches(db)
    except Exception:
        logging.exception('coverage scan failed')
        return []
def run_coverage(batch_id):
    with Session() as db:
        return coverage_service.process_batch(db,batch_id,OWNER)
def execute(id):
    with Session() as db:
        r=db.get(Run,id)
        if not r or r.state!='queued':return
        r.state='running';db.commit()
        try:
            result=calculate(r.snapshot['records'],r.parameters)
            json.dumps(result,allow_nan=False)
            r.result=result;r.state='completed'
            response=csv_response(result,'run.csv')
            (ARTIFACT_DIR/f'run-{r.id}.csv').write_bytes(response.body)
            log(db,'worker','run.completed',r.id)
        except Exception as e:
            logging.exception('analysis failed');r.state='failed';r.error=str(e)[:2000]
            log(db,'worker','run.failed',r.id,r.error)
        db.commit()
def main():
    for batch_id in _recover():
        try:run_coverage(batch_id)
        except Exception:logging.exception('coverage recovery run failed')
    last_scan=time.monotonic()
    while True:
        (ARTIFACT_DIR/'heartbeat').write_text(str(time.time()))
        picked=None
        try:
            picked=queue.blpop(['analysis:queue',coverage_service.COVERAGE_QUEUE],timeout=1)
        except Exception:
            logging.exception('queue unavailable');time.sleep(1)
        if picked:
            name,payload=picked[0].decode(),picked[1]
            if name==coverage_service.COVERAGE_QUEUE:
                try:run_coverage(int(payload))
                except Exception:logging.exception('coverage batch failed')
            else:
                execute(int(payload))
        if time.monotonic()-last_scan>=RESCAN_SECONDS:
            last_scan=time.monotonic()
            for batch_id in _scan():
                try:run_coverage(batch_id)
                except Exception:logging.exception('coverage scan run failed')
if __name__=='__main__':main()

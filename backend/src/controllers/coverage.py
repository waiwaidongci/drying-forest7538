from fastapi import HTTPException
from ..services import coverage
from ..services.runs import get_run
def _run(db,run_id):
    r=get_run(db,run_id)
    if not r:raise HTTPException(404,'未找到分析任务')
    return r
class CoverageController:
    @staticmethod
    def create(db,user,run_id,data):
        run=_run(db,run_id)
        summary=coverage.create_batch(db,user,run,data)
        if summary['state'] in ('queued','running'):
            coverage.enqueue(summary['id'])
        return summary
    @staticmethod
    def list(db,run_id):
        return coverage.list_batches(db,_run(db,run_id))
    @staticmethod
    def detail(db,run_id,batch_id):
        return coverage.batch_detail(db,_run(db,run_id),batch_id)

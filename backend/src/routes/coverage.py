from fastapi import APIRouter,Depends
from ..config.database import db_session
from ..schemas.coverage import CoverageInput
from ..services.auth import current_user,writer
from ..controllers.coverage import CoverageController as C
router=APIRouter()
@router.get('/runs/{run_id}/coverage')
def batches(run_id:int,db=Depends(db_session),u=Depends(current_user)):return C.list(db,run_id)
@router.post('/runs/{run_id}/coverage')
def create(run_id:int,data:CoverageInput,db=Depends(db_session),u=Depends(writer)):return C.create(db,u,run_id,data)
@router.get('/runs/{run_id}/coverage/{batch_id}')
def detail(run_id:int,batch_id:int,db=Depends(db_session),u=Depends(current_user)):return C.detail(db,run_id,batch_id)

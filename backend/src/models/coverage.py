from sqlalchemy import Column,Integer,String,JSON,Text,ForeignKey,UniqueConstraint,Index
from ..config.database import Base
from ..config.settings import CONFIG
class CoverageBatch(Base):
    __tablename__='coverage_batches'
    __table_args__=(
        UniqueConstraint('run_id','request_key',name='uq_coverage_request'),
    )
    id=Column(Integer,primary_key=True)
    run_id=Column(Integer,ForeignKey(CONFIG['run_table']+'.id'),nullable=False)
    request_key=Column(String(80),nullable=False)
    state=Column(String(20),default='queued',nullable=False)
    scale_digits=Column(Integer,nullable=False)
    basis_digest=Column(String(64),nullable=False)
    basis_count=Column(Integer,nullable=False)
    item_count=Column(Integer,nullable=False)
    created_by=Column(String(120),nullable=False)
    created_at=Column(String(40),nullable=False)
    updated_at=Column(String(40),nullable=False)
    completed_at=Column(String(40),default='')
    verified_at=Column(String(40),default='')
    error=Column(Text,default='')
class CoverageItem(Base):
    __tablename__='coverage_items'
    __table_args__=(
        UniqueConstraint('batch_id','position',name='uq_coverage_item'),
        Index('ix_coverage_items_batch','batch_id'),
    )
    id=Column(Integer,primary_key=True)
    batch_id=Column(Integer,ForeignKey('coverage_batches.id'),nullable=False)
    position=Column(Integer,nullable=False)
    label=Column(String(120),nullable=False)
    values=Column(JSON,nullable=False)
    state=Column(String(20),default='pending',nullable=False)
    proof=Column(JSON,nullable=True)
    attempts=Column(Integer,default=0,nullable=False)
    claimed_by=Column(String(120),default='',nullable=False)
    claimed_at=Column(String(40),default='')
    error=Column(Text,default='')

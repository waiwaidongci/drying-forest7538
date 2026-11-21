from decimal import Decimal
from pydantic import BaseModel,ConfigDict,Field
class ConditionInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    label:str=Field(min_length=1,max_length=120)
    moisture:Decimal
    thickness:Decimal
    temperature:Decimal
    airflow:Decimal
class CoverageInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    request_key:str=Field(min_length=8,max_length=80,pattern=r'^[A-Za-z0-9._:-]+$')
    items:list[ConditionInput]=Field(min_length=1,max_length=500)

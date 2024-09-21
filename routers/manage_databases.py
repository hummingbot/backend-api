import json

from typing import List, Dict, Any

from utils.etl_databases import HummingbotDatabase
from fastapi import APIRouter

from utils.file_system import FileSystemUtil

router = APIRouter(tags=["Database Management"])
file_system = FileSystemUtil()


@router.post("/list-databases", response_model=List[str])
async def list_databases():
    return file_system.list_databases()


@router.post("/read-databases", response_model=List[Dict[str, Any]])
async def read_databases(db_paths: List[str] = None):
    dbs = []
    for db_path in db_paths:
        db = HummingbotDatabase(db_path)
        try:
            db_content = {
                "db_name": db.db_name,
                "db_path": db.db_path,
                "healthy": db.status["general_status"],
                "status": db.status,
                "tables": {
                    "order": json.dumps(db.get_orders().to_dict()),
                    "trade_fill": json.dumps(db.get_trade_fills().to_dict()),
                    "executor": json.dumps(db.get_executors_data().to_dict()),
                    "order_status": json.dumps(db.get_order_status().to_dict())
                }
            }
        except Exception as e:
            print(f"Error reading database: {str(e)}")
            db_content = {
                "db_name": "",
                "db_path": db_path,
                "healthy": False,
                "status": db.status,
                "tables": {}
            }
        dbs.append(db_content)
    return dbs

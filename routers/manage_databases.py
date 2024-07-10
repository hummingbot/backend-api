import json
import time

import pandas as pd
from typing import List, Dict, Any

from services.etl_databases import HummingbotDatabase, ETLPerformance
from fastapi import APIRouter

from utils.file_system import FileSystemUtil

router = APIRouter(tags=["Database Management"])
file_system = FileSystemUtil()


@router.post("/list-databases", response_model=List[str])
async def list_databases(full_path: bool = False):
    return file_system.list_databases(full_path)


@router.post("/read-databases", response_model=List[Dict[str, Any]])
async def read_databases():
    dbs = []
    for db_path in file_system.list_databases(full_path=True):
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
            db_content = {
                "db_name": "",
                "db_path": db_path,
                "healthy": False,
                "status": db.status,
                "tables": {}
            }
        dbs.append(db_content)
    return dbs


@router.post("/upload-database")
async def upload_database():
    pass


@router.post("/create-checkpoint", response_model=Dict[str, Any])
async def create_checkpoint(db_names: List[str]):
    try:
        dbs = await read_databases()
        healthy_dbs = [db for db in dbs if db["healthy"] and db["db_name"] in db_names]
        trade_fill = pd.DataFrame()
        orders = pd.DataFrame()
        order_status = pd.DataFrame()
        executors = pd.DataFrame()
        for db in healthy_dbs:
            new_trade_fill = pd.DataFrame(json.loads(db["tables"]["trade_fill"]))
            new_trade_fill["db_path"] = db["db_path"]
            new_trade_fill["db_name"] = db["db_name"]

            new_orders = pd.DataFrame(json.loads(db["tables"]["order"]))
            new_orders["db_path"] = db["db_path"]
            new_orders["db_name"] = db["db_name"]

            new_order_status = pd.DataFrame(json.loads(db["tables"]["order_status"]))
            new_order_status["db_path"] = db["db_path"]
            new_order_status["db_name"] = db["db_name"]

            new_executors = pd.DataFrame(json.loads(db["tables"]["executor"]))
            new_executors["db_path"] = db["db_path"]
            new_executors["db_name"] = db["db_name"]

            trade_fill = pd.concat([trade_fill, new_trade_fill])
            orders = pd.concat([orders, new_orders])
            order_status = pd.concat([order_status, new_order_status])
            executors = pd.concat([executors, new_executors])

        tables_dict = {
            "trade_fill": trade_fill,
            "orders": orders,
            "order_status": order_status,
            "executors": executors,
        }

        etl = ETLPerformance(db_path=f"bots/data/checkpoint_{str(int(time.time()))}.sqlite")
        etl.create_tables()
        etl.insert_data(tables_dict)
        return {"message": "Checkpoint created successfully."}
    except Exception as e:
        return {"message": f"Error: {str(e)}"}


@router.post("/list-checkpoints", response_model=List[str])
async def list_checkpoints(full_path: bool):
    return file_system.list_checkpoints(full_path)


@router.post("/load-checkpoint")
async def load_checkpoint(checkpoint_path: str):
    try:
        etl = ETLPerformance(checkpoint_path)
        executor = etl.load_executors()
        order = etl.load_orders()
        trade_fill = etl.load_trade_fill()
        checkpoint_data = {
            "executor": json.dumps(executor.to_dict()),
            "order": json.dumps(order.to_dict()),
            "trade_fill": json.dumps(trade_fill.to_dict()),
        }
        return checkpoint_data
    except Exception as e:
        return {"message": f"Error: {str(e)}"}

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from deps import get_bots_orchestrator
from services.bots_orchestrator import BotsOrchestrator
from utils.file_system import fs_util
from utils.hummingbot_database_reader import HummingbotDatabase

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Archived Bots"], prefix="/archived-bots")


def _validate_db_path(db_path: str) -> str:
    """
    Resolve db_path and ensure it points to a database file inside the archived bots directory.

    Raises HTTPException 400 for paths escaping the archived directory and 404 for missing files.
    """
    try:
        return fs_util.get_archived_db_path(db_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/", response_model=List[str])
async def list_databases():
    """
    List all available database files in the system.

    Returns:
        List of database file paths
    """
    return fs_util.list_databases()


@router.delete("/{db_path:path}")
async def delete_archived_bot(
    db_path: str,
    bots_orchestrator: BotsOrchestrator = Depends(get_bots_orchestrator)
):
    """
    Delete an archived bot and its entire directory.
    Also attempts to delete matching BotRun records from PostgreSQL (best-effort).

    Args:
        db_path: Path to the database file (as returned by list_databases)

    Returns:
        Confirmation message with the deleted bot name and count of cleaned PG records
    """
    try:
        bot_name = fs_util.delete_archived_bot(db_path)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=f"Permission denied: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error deleting archived bot: {str(e)}")

    # Best-effort: also clean matching BotRun records from PG. Bot-run persistence is
    # the orchestrator's (ARCH-035), so the session and the swallow live there.
    bot_runs_deleted = await bots_orchestrator.delete_bot_runs_for_bot(bot_name)

    return {
        "message": f"Archived bot '{bot_name}' deleted successfully",
        "bot_name": bot_name,
        "bot_runs_deleted": bot_runs_deleted
    }


@router.get("/{db_path:path}/status")
async def get_database_status(db_path: str):
    """
    Get status information for a specific database.

    Args:
        db_path: Path to the database file

    Returns:
        Database status including table health
    """
    resolved_db_path = _validate_db_path(db_path)
    try:
        db = HummingbotDatabase(resolved_db_path)
        return {
            "db_path": db_path,
            "status": db.status,
            "healthy": db.status["general_status"]
        }
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Database not found or error: {str(e)}")


@router.get("/{db_path:path}/summary")
async def get_database_summary(db_path: str):
    """
    Get a summary of database contents including basic statistics.

    Args:
        db_path: Full path to the database file

    Returns:
        Summary statistics of the database contents
    """
    resolved_db_path = _validate_db_path(db_path)
    try:
        db = HummingbotDatabase(resolved_db_path)

        # Get basic counts
        orders = db.get_orders()
        trades = db.get_trade_fills()
        executors = db.get_executors_data()
        positions = db.get_positions()
        controllers = db.get_controllers_data()

        return {
            "db_path": db_path,
            "total_orders": len(orders),
            "total_trades": len(trades),
            "total_executors": len(executors),
            "total_positions": len(positions),
            "total_controllers": len(controllers),
            "trading_pairs": orders["trading_pair"].unique().tolist() if len(orders) > 0 else [],
            "exchanges": orders["connector_name"].unique().tolist() if len(orders) > 0 else [],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error analyzing database: {str(e)}")


@router.get("/{db_path:path}/performance")
async def get_database_performance(db_path: str):
    """
    Get trade-based performance analysis for a bot database.

    Args:
        db_path: Full path to the database file

    Returns:
        Trade-based performance metrics with rolling calculations
    """
    resolved_db_path = _validate_db_path(db_path)
    try:
        db = HummingbotDatabase(resolved_db_path)

        # Use new trade-based performance calculation
        performance_data = db.calculate_trade_based_performance()

        if len(performance_data) == 0:
            return {
                "db_path": db_path,
                "error": "No trades found in database",
                "performance_data": []
            }

        # Convert to records for JSON response
        performance_records = performance_data.fillna(0).to_dict('records')

        # Calculate summary statistics
        final_row = performance_data.iloc[-1] if len(performance_data) > 0 else {}
        summary = {
            "total_trades": len(performance_data),
            "final_net_pnl_quote": float(final_row.get('net_pnl_quote', 0)),
            "final_realized_pnl_quote": float(final_row.get('realized_trade_pnl_quote', 0)),
            "final_unrealized_pnl_quote": float(final_row.get('unrealized_trade_pnl_quote', 0)),
            "total_fees_quote": float(performance_data['fees_quote'].sum()),
            "total_volume_quote": float(
                performance_data['cum_volume_quote'].iloc[-1] if len(performance_data) > 0 else 0),
            "final_net_position": float(final_row.get('net_position', 0)),
            "trading_pairs": performance_data['trading_pair'].unique().tolist(),
            "connector_names": performance_data['connector_name'].unique().tolist()
        }

        return {
            "db_path": db_path,
            "summary": summary,
            "performance_data": performance_records
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error calculating performance: {str(e)}")


@router.get("/{db_path:path}/trades")
async def get_database_trades(
        db_path: str,
        limit: int = Query(default=100, description="Limit number of trades returned"),
        offset: int = Query(default=0, description="Offset for pagination")
):
    """
    Get trade history from a database.

    Args:
        db_path: Full path to the database file
        limit: Maximum number of trades to return
        offset: Offset for pagination

    Returns:
        List of trades with pagination info
    """
    resolved_db_path = _validate_db_path(db_path)
    try:
        db = HummingbotDatabase(resolved_db_path)
        trades = db.get_trade_fills()

        # Apply pagination
        total_trades = len(trades)
        trades_page = trades.iloc[offset:offset + limit]

        return {
            "db_path": db_path,
            "trades": trades_page.fillna(0).to_dict('records'),
            "pagination": {
                "total": total_trades,
                "limit": limit,
                "offset": offset,
                "has_more": offset + limit < total_trades
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching trades: {str(e)}")


@router.get("/{db_path:path}/orders")
async def get_database_orders(
        db_path: str,
        limit: int = Query(default=100, description="Limit number of orders returned"),
        offset: int = Query(default=0, description="Offset for pagination"),
        status: Optional[str] = Query(default=None, description="Filter by order status")
):
    """
    Get order history from a database.

    Args:
        db_path: Full path to the database file
        limit: Maximum number of orders to return
        offset: Offset for pagination
        status: Optional status filter

    Returns:
        List of orders with pagination info
    """
    resolved_db_path = _validate_db_path(db_path)
    try:
        db = HummingbotDatabase(resolved_db_path)
        orders = db.get_orders()

        # Apply status filter if provided
        if status:
            orders = orders[orders["last_status"] == status]

        # Apply pagination
        total_orders = len(orders)
        orders_page = orders.iloc[offset:offset + limit]

        return {
            "db_path": db_path,
            "orders": orders_page.fillna(0).to_dict('records'),
            "pagination": {
                "total": total_orders,
                "limit": limit,
                "offset": offset,
                "has_more": offset + limit < total_orders
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching orders: {str(e)}")


@router.get("/{db_path:path}/executors")
async def get_database_executors(db_path: str):
    """
    Get executor data from a database.

    Args:
        db_path: Full path to the database file

    Returns:
        List of executors with their configurations and results
    """
    resolved_db_path = _validate_db_path(db_path)
    try:
        db = HummingbotDatabase(resolved_db_path)
        executors = db.get_executors_data()

        return {
            "db_path": db_path,
            "executors": executors.fillna(0).to_dict('records'),
            "total": len(executors)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching executors: {str(e)}")


@router.get("/{db_path:path}/positions")
async def get_database_positions(
        db_path: str,
        limit: int = Query(default=100, description="Limit number of positions returned"),
        offset: int = Query(default=0, description="Offset for pagination")
):
    """
    Get position data from a database.

    Args:
        db_path: Full path to the database file
        limit: Maximum number of positions to return
        offset: Offset for pagination

    Returns:
        List of positions with pagination info
    """
    resolved_db_path = _validate_db_path(db_path)
    try:
        db = HummingbotDatabase(resolved_db_path)
        positions = db.get_positions()

        # Apply pagination
        total_positions = len(positions)
        positions_page = positions.iloc[offset:offset + limit]

        return {
            "db_path": db_path,
            "positions": positions_page.fillna(0).to_dict('records'),
            "pagination": {
                "total": total_positions,
                "limit": limit,
                "offset": offset,
                "has_more": offset + limit < total_positions
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching positions: {str(e)}")


@router.get("/{db_path:path}/controllers")
async def get_database_controllers(db_path: str):
    """
    Get controller data from a database.

    Args:
        db_path: Full path to the database file

    Returns:
        List of controllers that were running with their configurations
    """
    resolved_db_path = _validate_db_path(db_path)
    try:
        db = HummingbotDatabase(resolved_db_path)
        controllers = db.get_controllers_data()

        return {
            "db_path": db_path,
            "controllers": controllers.fillna(0).to_dict('records'),
            "total": len(controllers)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching controllers: {str(e)}")

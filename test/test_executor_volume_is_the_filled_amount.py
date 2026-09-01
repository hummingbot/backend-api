"""Volume and filled amount are one number, on every executor type.

They used to be two. `filled_amount_quote` meant "the amount this executor filled",
which for an order-placing executor IS its volume but for an LP executor was the
CAPITAL IT DEPOSITED — so a position that put up $200 and traded nothing reported $200
of volume the moment it opened. The fix at the time was a second field,
`volume_traded_quote`, carried through the executor, the ExecutorInfo, this API's
schema, its database and a migration.

That second field is gone. `lp_executor.filled_amount_quote` now derives the volume
that crossed the position from the fees it earned, so one field means the same thing
everywhere and an LP executor sums like with like against every other kind. It also
removes the reason this API had to require a core surface a released hummingbot did not
carry — see utils/core_compatibility.py.

What is left to pin here is that nothing in this API re-introduces the split by summing
the wrong column.
"""

import inspect
import re
from unittest.mock import MagicMock

from database.models import ExecutorPerformanceSnapshot, ExecutorRecord
from database.repositories.executor_repository import ExecutorRepository
from services.executor_service import ExecutorService


def test_the_record_has_no_separate_volume_column():
    assert not hasattr(ExecutorRecord, "volume_traded_quote"), (
        "the executors table grew a second volume column again; filled_amount_quote is it"
    )


def test_the_performance_snapshot_has_no_separate_volume_column():
    """The same rule on the table FEAT-001 added, which mirrors these four metrics."""
    assert not hasattr(ExecutorPerformanceSnapshot, "volume_traded_quote"), (
        "the snapshot table grew a second volume column; filled_amount_quote is it"
    )


def test_every_aggregate_sums_the_filled_amount():
    """Three aggregates summed the old column; a fourth added later would too."""
    source = inspect.getsource(ExecutorRepository.get_performance_report)
    summed = set(re.findall(r"func\.sum\(ExecutorRecord\.(\w+)\)", source))

    assert "filled_amount_quote" in summed
    assert "volume_traded_quote" not in summed


def _service_with(executor_info_fields):
    """An ExecutorService wired to one fake LP executor, with nothing else running."""
    service = ExecutorService.__new__(ExecutorService)
    service._executor_metadata = {"e-1": {"executor_type": "lp_executor"}}
    service._log_capture = MagicMock()
    service._log_capture.get_error_count.return_value = 0
    service._log_capture.get_last_error.return_value = None

    executor = MagicMock()
    info = MagicMock()
    info.model_dump.return_value = {"custom_info": {}, **executor_info_fields}
    info.side = None
    executor.executor_info = info
    executor.status.name = "TERMINATED"
    executor.close_type = None
    executor.is_closed = True
    service._active_executors = {"e-1": executor}
    return service


def test_the_active_summary_counts_the_volume_the_executor_reported():
    """An LP position that traded $2,500 through its range reports $2,500.

    The executor derives that from its fees; this API just sums what it is given.
    """
    service = _service_with({"filled_amount_quote": 2500.0})

    assert service.get_summary()["total_volume_quote"] == 2500.0


def test_a_funded_position_that_traded_nothing_summarises_as_no_volume():
    """The original defect, still pinned: capital deposited is not volume.

    It is enforced upstream now -- an LP executor with no fees derives no volume -- so
    what this asserts is that the API does not put the deposit back by another route.
    """
    service = _service_with({"filled_amount_quote": 0.0})

    assert service.get_summary()["total_volume_quote"] == 0.0

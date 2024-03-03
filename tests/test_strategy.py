from datetime import datetime, timedelta

import pytest
from hexbytes import HexBytes
from twisted.logger import LogLevel, globalLogPublisher
from web3.types import TxParams

from atxm.exceptions import Fault, TransactionFaulted
from atxm.strategies import TimeoutStrategy
from atxm.tx import PendingTx


def test_timeout_strategy(w3, mocker):
    TIMEOUT = 600  # 10 mins

    # default timeout
    timeout_strategy = TimeoutStrategy(w3)
    assert timeout_strategy.timeout == TimeoutStrategy._TIMEOUT

    # specific timeout
    timeout_strategy = TimeoutStrategy(w3, timeout=TIMEOUT)
    assert timeout_strategy.timeout == TIMEOUT

    assert timeout_strategy.name == timeout_strategy._NAME

    # None tx does not time out
    with pytest.raises(RuntimeError):
        assert timeout_strategy.execute(None)

    # mock pending tx
    pending_tx = mocker.Mock(spec=PendingTx)
    pending_tx.txhash = HexBytes("0xdeadbeef")
    params = mocker.Mock(spec=TxParams)
    pending_tx.params = params
    now = datetime.now()
    pending_tx.created = now.timestamp()

    # can't mock datetime, so instead use creation time to manipulate scenarios

    # 1) tx just created a does not time out
    for i in range(3):
        assert timeout_strategy.execute(pending_tx) is None  # no change to params

    # 2) remaining time is < warn factor; still doesn't time out but we warn about it
    pending_tx.created = (
        now - timedelta(seconds=(TIMEOUT * (1 - timeout_strategy._WARN_FACTOR) + 1))
    ).timestamp()

    warnings = []

    def warning_trapper(event):
        if event["log_level"] == LogLevel.warn:
            warnings.append(event)

    globalLogPublisher.addObserver(warning_trapper)
    assert timeout_strategy.execute(pending_tx) is None  # no change to params
    globalLogPublisher.removeObserver(warning_trapper)

    assert len(warnings) == 1
    warning = warnings[0]["log_format"]
    assert f"[timeout] Transaction {pending_tx.txhash.hex()} will timeout in" in warning

    # 3) close to timeout but not quite (5s short)
    pending_tx.created = (now - timedelta(seconds=(TIMEOUT - 5))).timestamp()
    assert timeout_strategy.execute(pending_tx) is None  # no change to params

    # 4) timeout
    pending_tx.created = (now - timedelta(seconds=(TIMEOUT + 1))).timestamp()
    with pytest.raises(TransactionFaulted) as exc_info:
        timeout_strategy.execute(pending_tx)
    e = exc_info.value
    e.tx = pending_tx
    e.fault = Fault.TIMEOUT
    e.message = "Transaction has timed out"

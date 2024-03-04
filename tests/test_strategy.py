import math
from datetime import datetime, timedelta

import pytest
from hexbytes import HexBytes
from twisted.logger import LogLevel, globalLogPublisher
from web3.types import TxParams

from atxm.exceptions import Fault, TransactionFaulted
from atxm.strategies import FixedRateSpeedUp, TimeoutStrategy
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


def test_speedup_strategy_constructor(w3):
    # invalid increase percentage
    for speedup_perc in [-1, -0.24, 0, 1.01, 1.1]:
        with pytest.raises(ValueError):
            _ = FixedRateSpeedUp(w3=w3, speedup_increase_percentage=speedup_perc)

    # invalid max tip
    for max_tip in [-1, 0, 0.5, 0.9, 1]:
        with pytest.raises(ValueError):
            _ = FixedRateSpeedUp(w3=w3, max_tip_factor=max_tip)

    # defaults
    speedup_strategy = FixedRateSpeedUp(w3=w3)
    assert speedup_strategy.speedup_factor == (
        1 + FixedRateSpeedUp._SPEEDUP_INCREASE_PERCENTAGE
    )
    assert speedup_strategy.max_tip_factor == FixedRateSpeedUp._MAX_TIP_FACTOR

    # other values
    speedup_increase = 0.223
    max_tip_factor = 4
    speedup_strategy = FixedRateSpeedUp(
        w3=w3,
        speedup_increase_percentage=speedup_increase,
        max_tip_factor=max_tip_factor,
    )
    assert speedup_strategy.speedup_factor == (1 + speedup_increase)
    assert speedup_strategy.max_tip_factor == max_tip_factor


def test_speedup_strategy_legacy_tx(w3, legacy_transaction, mocker):
    speedup_percentage = 0.112  # 11.2%
    speedup_strategy = FixedRateSpeedUp(
        w3, speedup_increase_percentage=speedup_percentage
    )
    assert speedup_strategy.name == "speedup"

    transaction_count = legacy_transaction["nonce"]
    mocker.patch.object(w3.eth, "get_transaction_count", return_value=transaction_count)
    pending_tx = mocker.Mock(spec=PendingTx)
    pending_tx.id = 1

    # legacy transaction - keep speeding up

    # generated gas price < existing tx gas price
    generated_gas_price = legacy_transaction["gasPrice"] - 1  # < what is in tx
    mocker.patch.object(w3.eth, "generate_gas_price", return_value=generated_gas_price)

    assert legacy_transaction["gasPrice"]
    tx_params = dict(legacy_transaction)
    for i in range(3):
        pending_tx.params = tx_params
        old_gas_price = tx_params["gasPrice"]
        old_nonce = tx_params["nonce"]
        tx_params = speedup_strategy.execute(pending_tx)

        current_gas_price = tx_params["gasPrice"]
        assert current_gas_price != old_gas_price
        assert current_gas_price == math.ceil(old_gas_price * (1 + speedup_percentage))
        assert tx_params["nonce"] == old_nonce

    # generated gas price is None - same results as before
    mocker.patch.object(w3.eth, "generate_gas_price", return_value=None)  # set to None
    for i in range(3):
        tx_params = dict(legacy_transaction)
        pending_tx.params = tx_params
        old_gas_price = tx_params["gasPrice"]
        old_nonce = tx_params["nonce"]
        tx_params = speedup_strategy.execute(pending_tx)

        current_gas_price = tx_params["gasPrice"]
        assert current_gas_price != old_gas_price
        assert current_gas_price == math.ceil(old_gas_price * (1 + speedup_percentage))
        assert tx_params["nonce"] == old_nonce

    # increase generated gas price more than existing gas price in legacy tx
    tx_params = dict(legacy_transaction)
    pending_tx.params = tx_params
    generated_gas_price = tx_params["gasPrice"] * 2  # > what is in tx
    mocker.patch.object(w3.eth, "generate_gas_price", return_value=generated_gas_price)

    old_gas_price = tx_params["gasPrice"]
    old_nonce = tx_params["nonce"]
    updated_tx_params = speedup_strategy.execute(pending_tx)

    current_gas_price = updated_tx_params["gasPrice"]
    assert current_gas_price != old_gas_price
    assert current_gas_price == math.ceil(
        generated_gas_price * (1 + speedup_percentage)
    )
    assert updated_tx_params["nonce"] == old_nonce

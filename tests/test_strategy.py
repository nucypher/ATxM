import math
import random
from datetime import datetime, timedelta
from unittest.mock import PropertyMock

import pytest
from hexbytes import HexBytes
from twisted.logger import LogLevel, globalLogPublisher
from web3.types import TxParams

from atxm.exceptions import Fault, TransactionFaulted
from atxm.strategies import ExponentialSpeedupStrategy, TimeoutStrategy
from atxm.tx import PendingTx


def test_timeout_strategy(w3, mocker):
    TIMEOUT = 900  # 15 mins

    # default timeout
    timeout_strategy = TimeoutStrategy(w3)
    assert timeout_strategy.timeout == TimeoutStrategy._TIMEOUT
    assert (
        timeout_strategy._warn_threshold
        == TimeoutStrategy._TIMEOUT * TimeoutStrategy._WARN_FACTOR
    )

    # specific timeout - low timeout
    low_timeout = 60  # 60s
    timeout_strategy = TimeoutStrategy(w3, timeout=low_timeout)
    assert timeout_strategy.timeout == low_timeout
    assert (
        timeout_strategy._warn_threshold == 30
    )  # timeout so low that max warn threshold hit

    # specific timeout
    timeout_strategy = TimeoutStrategy(w3, timeout=TIMEOUT)
    assert timeout_strategy.timeout == TIMEOUT
    assert timeout_strategy._warn_threshold == TIMEOUT * TimeoutStrategy._WARN_FACTOR

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
            _ = ExponentialSpeedupStrategy(
                w3=w3, speedup_increase_percentage=speedup_perc
            )

    # invalid max tip
    for max_tip in [-1, 0, 0.5, 0.9, 1]:
        with pytest.raises(ValueError):
            _ = ExponentialSpeedupStrategy(w3=w3, max_tip_factor=max_tip)

    # defaults
    speedup_strategy = ExponentialSpeedupStrategy(w3=w3)
    assert speedup_strategy.speedup_factor == (
        1 + ExponentialSpeedupStrategy._SPEEDUP_INCREASE_PERCENTAGE
    )
    assert speedup_strategy.max_tip_factor == ExponentialSpeedupStrategy._MAX_TIP_FACTOR

    # other values
    speedup_increase = 0.223
    max_tip_factor = 4
    speedup_strategy = ExponentialSpeedupStrategy(
        w3=w3,
        speedup_increase_percentage=speedup_increase,
        max_tip_factor=max_tip_factor,
    )
    assert speedup_strategy.speedup_factor == (1 + speedup_increase)
    assert speedup_strategy.max_tip_factor == max_tip_factor

    assert speedup_strategy.name == "speedup"


def test_speedup_strategy_legacy_tx(w3, legacy_transaction, mocker):
    speedup_percentage = 0.112  # 11.2%
    speedup_strategy = ExponentialSpeedupStrategy(
        w3, speedup_increase_percentage=speedup_percentage
    )

    pending_tx = mocker.Mock(spec=PendingTx)
    pending_tx.id = 1

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


def test_speedup_strategy_eip1559_tx_no_blockchain_change(
    w3, eip1559_transaction, mocker
):
    # blockchain conditions have not changed since
    (
        current_base_fee,
        max_tip_factor,
        pending_tx,
        speedup_percentage,
        speedup_strategy,
        suggested_tip,
    ) = eip1559_setup(mocker, w3)

    old_max_priority_fee_per_gas = eip1559_transaction["maxPriorityFeePerGas"]
    old_max_fee_per_gas = eip1559_transaction["maxFeePerGas"]

    pending_tx.params = dict(eip1559_transaction)
    tx_params = speedup_strategy.execute(pending_tx)

    updated_max_priority_fee_per_gas = tx_params["maxPriorityFeePerGas"]
    assert updated_max_priority_fee_per_gas > old_max_priority_fee_per_gas
    assert updated_max_priority_fee_per_gas == math.ceil(
        old_max_priority_fee_per_gas * (1 + speedup_percentage)
    )

    updated_max_fee_per_gas = tx_params["maxFeePerGas"]
    assert updated_max_fee_per_gas > old_max_fee_per_gas
    assert updated_max_fee_per_gas == math.ceil(
        old_max_fee_per_gas * (1 + speedup_percentage)
    )
    assert updated_max_fee_per_gas >= (
        current_base_fee + updated_max_priority_fee_per_gas
    )


def test_speedup_strategy_eip1559_tx_base_fee_decreased(
    w3, eip1559_transaction, mocker
):
    # 2) suppose current base fee decreased; calc remains simple
    (
        current_base_fee,
        max_tip_factor,
        pending_tx,
        speedup_percentage,
        speedup_strategy,
        suggested_tip,
    ) = eip1559_setup(mocker, w3)

    old_max_priority_fee_per_gas = eip1559_transaction["maxPriorityFeePerGas"]
    old_max_fee_per_gas = eip1559_transaction["maxFeePerGas"]

    new_current_base_fee = math.ceil(current_base_fee * 0.95)
    mocker.patch.object(
        w3.eth, "get_block", return_value={"baseFeePerGas": new_current_base_fee}
    )

    pending_tx.params = dict(eip1559_transaction)
    tx_params = speedup_strategy.execute(pending_tx)

    updated_max_priority_fee_per_gas = tx_params["maxPriorityFeePerGas"]
    assert updated_max_priority_fee_per_gas > old_max_priority_fee_per_gas
    assert updated_max_priority_fee_per_gas == math.ceil(
        old_max_priority_fee_per_gas * (1 + speedup_percentage)
    )

    updated_max_fee_per_gas = tx_params["maxFeePerGas"]
    assert updated_max_fee_per_gas > old_max_fee_per_gas
    assert updated_max_fee_per_gas == math.ceil(
        old_max_fee_per_gas * (1 + speedup_percentage)
    )
    assert updated_max_fee_per_gas >= (
        new_current_base_fee + updated_max_priority_fee_per_gas
    )


def test_speedup_strategy_eip1559_tx_base_fee_increased(
    w3, eip1559_transaction, mocker
):
    # suppose current base fee increase; it gets used instead of prior value
    (
        current_base_fee,
        max_tip_factor,
        pending_tx,
        speedup_percentage,
        speedup_strategy,
        suggested_tip,
    ) = eip1559_setup(mocker, w3)

    old_max_priority_fee_per_gas = eip1559_transaction["maxPriorityFeePerGas"]
    old_max_fee_per_gas = eip1559_transaction["maxFeePerGas"]

    new_current_base_fee = math.ceil(current_base_fee * 1.1)
    mocker.patch.object(
        w3.eth, "get_block", return_value={"baseFeePerGas": new_current_base_fee}
    )

    pending_tx.params = dict(eip1559_transaction)
    tx_params = speedup_strategy.execute(pending_tx)

    updated_max_priority_fee_per_gas = tx_params["maxPriorityFeePerGas"]
    assert updated_max_priority_fee_per_gas > old_max_priority_fee_per_gas
    assert updated_max_priority_fee_per_gas == math.ceil(
        old_max_priority_fee_per_gas * (1 + speedup_percentage)
    )

    updated_max_fee_per_gas = tx_params["maxFeePerGas"]
    assert updated_max_fee_per_gas > old_max_fee_per_gas
    assert updated_max_fee_per_gas > math.ceil(
        old_max_fee_per_gas * (1 + speedup_percentage)
    )
    assert updated_max_fee_per_gas == math.ceil(
        new_current_base_fee * (1 + speedup_percentage)
        + updated_max_priority_fee_per_gas
    )


def test_speedup_strategy_eip1559_tx_no_max_fee(w3, eip1559_transaction, mocker):
    # suppose no maxFeePerGas specified - use default calc (same as web3py)
    (
        current_base_fee,
        max_tip_factor,
        pending_tx,
        speedup_percentage,
        speedup_strategy,
        suggested_tip,
    ) = eip1559_setup(mocker, w3)

    old_max_priority_fee_per_gas = eip1559_transaction["maxPriorityFeePerGas"]

    mocker.patch.object(
        w3.eth, "get_block", return_value={"baseFeePerGas": current_base_fee}
    )
    tx_params = dict(eip1559_transaction)
    del tx_params["maxFeePerGas"]

    pending_tx.params = tx_params
    tx_params = speedup_strategy.execute(pending_tx)

    updated_max_priority_fee_per_gas = tx_params["maxPriorityFeePerGas"]
    assert updated_max_priority_fee_per_gas == math.ceil(
        old_max_priority_fee_per_gas * (1 + speedup_percentage)
    )

    updated_max_fee_per_gas = tx_params["maxFeePerGas"]
    assert updated_max_fee_per_gas == math.ceil(
        (current_base_fee * 2) + updated_max_priority_fee_per_gas
    )


def test_speedup_strategy_eip1559_tx_no_max_priority_fee(
    w3, eip1559_transaction, mocker
):
    # suppose no maxPriorityFeePerGas specified - use suggested tip
    (
        current_base_fee,
        max_tip_factor,
        pending_tx,
        speedup_percentage,
        speedup_strategy,
        suggested_tip,
    ) = eip1559_setup(mocker, w3)

    mocker.patch.object(
        w3.eth, "get_block", return_value={"baseFeePerGas": current_base_fee}
    )
    tx_params = dict(eip1559_transaction)
    del tx_params["maxPriorityFeePerGas"]

    pending_tx.params = tx_params
    tx_params = speedup_strategy.execute(pending_tx)

    updated_max_priority_fee_per_gas = tx_params["maxPriorityFeePerGas"]
    assert updated_max_priority_fee_per_gas == math.ceil(
        suggested_tip * (1 + speedup_percentage)
    )

    updated_max_fee_per_gas = tx_params["maxFeePerGas"]
    assert updated_max_fee_per_gas == math.ceil(
        current_base_fee * (1 + speedup_percentage) + updated_max_priority_fee_per_gas
    )


def test_speedup_strategy_eip1559_tx_hit_max_tip(w3, eip1559_transaction, mocker):
    (
        current_base_fee,
        max_tip_factor,
        pending_tx,
        speedup_percentage,
        speedup_strategy,
        suggested_tip,
    ) = eip1559_setup(mocker, w3)

    pending_tx.params = dict(eip1559_transaction)
    expected_num_iterations_before_hitting_max_tip = math.ceil(
        math.log(max_tip_factor) / math.log(1 + speedup_percentage)
    )

    # do one less iteration than expected
    for i in range(expected_num_iterations_before_hitting_max_tip - 1):
        tx_params = speedup_strategy.execute(pending_tx)
        assert tx_params is not None
        assert tx_params["maxPriorityFeePerGas"] <= (suggested_tip * max_tip_factor)
        # update params
        pending_tx.params = tx_params

    # next attempt should cause tip to exceed max tip factor
    assert (pending_tx.params["maxPriorityFeePerGas"] * (1 + speedup_percentage)) > (
        suggested_tip * max_tip_factor
    )
    tx_params = speedup_strategy.execute(pending_tx)
    # hit max factor so no more changes - return None
    assert (
        tx_params is None
    ), f"no updates after {expected_num_iterations_before_hitting_max_tip} iterations"


def eip1559_setup(mocker, w3):
    speedup_percentage = round(random.randint(110, 230) / 1000, 3)  # [11%, 23%]
    max_tip_factor = random.randint(2, 5)
    speedup_strategy = ExponentialSpeedupStrategy(
        w3,
        speedup_increase_percentage=speedup_percentage,
        max_tip_factor=max_tip_factor,
    )
    pending_tx = mocker.Mock(spec=PendingTx)
    pending_tx.id = 1
    pending_tx.txhash = HexBytes("0xdeadbeef")

    # use consistent mocked values
    current_base_fee = w3.eth.get_block("latest")["baseFeePerGas"]
    suggested_tip = w3.eth.max_priority_fee
    mocker.patch.object(
        w3.eth, "get_block", return_value={"baseFeePerGas": current_base_fee}
    )
    mocker.patch(
        "web3.eth.eth.Eth.max_priority_fee", PropertyMock(return_value=suggested_tip)
    )
    return (
        current_base_fee,
        max_tip_factor,
        pending_tx,
        speedup_percentage,
        speedup_strategy,
        suggested_tip,
    )

import time

import math
from typing import List

import pytest

import pytest_twisted
from eth_account import Account
from eth_utils import ValidationError
from twisted.internet import reactor
from twisted.internet.task import deferLater
from web3.exceptions import (
    ProviderConnectionError,
    TimeExhausted,
    TooManyRequests,
    Web3Exception,
)

from atxm import AutomaticTxMachine
from atxm.strategies import AsyncTxStrategy, TimeoutStrategy
from atxm.tx import FaultedTx, FinalizedTx, FutureTx, PendingTx
from atxm.utils import _is_recoverable_send_tx_error


@pytest.fixture()
def rpc_spy(mocker, w3):
    spy = mocker.spy(w3, "eth")
    return spy


@pytest_twisted.inlineCallbacks
def test_no_rpc_calls_when_idle(clock, machine, state_observer, rpc_spy):
    assert machine.current_state == machine._IDLE
    assert not machine.busy
    assert len(machine.queued) == 0

    machine.start(now=True)
    for i in range(3):
        yield clock.advance(1)
        assert machine.current_state == machine._IDLE

    # Verify that no RPC calls were made
    assert rpc_spy.call_count == 0

    assert not machine.busy
    assert len(machine.queued) == 0

    assert machine.current_state == machine._IDLE
    assert len(state_observer.transitions) == 0  # remained idle

    machine.stop()


def test_queue_from_parameter_handling(
    machine,
    account,
    eip1559_transaction,
    mock_wake_sleep,
    mocker,
):
    # 1. "from" parameter does not match account
    with pytest.raises(ValueError):
        tx_params = dict(eip1559_transaction)

        # use random checksum address
        random_account = Account.create(
            "YOUTH IS WASTED ON THE YOUNG"
        )  # - George Bernard Shaw
        assert random_account.address != account.address, "addresses don't match"

        tx_params["from"] = random_account.address
        _ = machine.queue_transaction(
            params=tx_params,
            signer=account,
            on_broadcast_failure=mocker.Mock(),
            on_fault=mocker.Mock(),
            on_finalized=mocker.Mock(),
        )

    # 2. no "from" parameter
    tx_params = dict(eip1559_transaction)
    if "from" in eip1559_transaction:
        del tx_params["from"]

    atx = machine.queue_transaction(
        params=tx_params,
        signer=account,
        on_broadcast_failure=mocker.Mock(),
        on_fault=mocker.Mock(),
        on_finalized=mocker.Mock(),
    )
    assert atx.params["from"] == account.address, "same as signer account"

    # 3. matching "from" parameter
    tx_params = dict(eip1559_transaction)
    tx_params["from"] = account.address
    atx = machine.queue_transaction(
        params=tx_params,
        signer=account,
        on_broadcast_failure=mocker.Mock(),
        on_fault=mocker.Mock(),
        on_finalized=mocker.Mock(),
    )
    assert atx.params["from"] == account.address


def test_queue(
    machine,
    state_observer,
    rpc_spy,
    account,
    eip1559_transaction,
    mock_wake_sleep,
    mocker,
):
    wake, _ = mock_wake_sleep

    # The machine is idle
    assert machine.current_state == machine._IDLE
    assert not machine.running
    assert not machine.busy

    # Queue a transaction
    atx = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
        on_broadcast_failure=mocker.Mock(),
        on_fault=mocker.Mock(),
        on_finalized=mocker.Mock(),
    )

    assert isinstance(atx, FutureTx)

    # The machine tries to start (but it's mocked!)
    assert wake.call_count == 1
    assert machine.busy

    # Nothing is pending
    assert machine.pending is None

    # There is one queued transaction
    assert len(machine.queued) == 1
    queued = machine.queued[0]
    assert queued._from == account.address
    assert queued.id == 0

    assert machine.current_state == machine._IDLE
    assert len(state_observer.transitions) == 0  # nothing actually executed


def test_wake_after_queuing_when_idle_and_not_already_running(
    machine,
    eip1559_transaction,
    account,
    mocker,
):
    assert machine.current_state == machine._IDLE
    assert not machine.busy

    stop_spy = mocker.spy(machine._task, "stop")
    start_spy = mocker.spy(machine._task, "start")

    assert not machine.running

    # Queue a transaction
    _ = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
        info={"message": "something wonderful is happening..."},
        on_broadcast_failure=mocker.Mock(),
        on_fault=mocker.Mock(),
        on_finalized=mocker.Mock(),
    )

    assert stop_spy.call_count == 0, "no task to stop"
    assert start_spy.call_count == 1, "task started"

    assert machine.running
    assert machine.current_state == machine._BUSY

    machine.stop()


def test_wake_after_queuing_when_idle_and_already_running(
    machine,
    eip1559_transaction,
    account,
    mocker,
):
    machine.start(now=True)

    stop_spy = mocker.spy(machine._task, "stop")
    start_spy = mocker.spy(machine._task, "start")

    assert machine.current_state == machine._IDLE
    assert not machine.busy

    assert machine.running

    # Queue a transaction
    _ = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
        info={"message": "something wonderful is happening..."},
        on_broadcast_failure=mocker.Mock(),
        on_fault=mocker.Mock(),
        on_finalized=mocker.Mock(),
    )

    assert stop_spy.call_count == 1, "task stopped"
    assert start_spy.call_count == 1, "task started"

    assert machine.running
    assert machine.current_state == machine._BUSY

    machine.stop()


def test_wake_no_call_after_queuing_when_already_busy(
    machine,
    eip1559_transaction,
    account,
    mock_wake_sleep,
    mocker,
):
    wake, _ = mock_wake_sleep

    assert machine.current_state == machine._IDLE

    # Queue a transaction
    _ = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
        info={"message": "something wonderful is happening..."},
        on_broadcast_failure=mocker.Mock(),
        on_fault=mocker.Mock(),
        on_finalized=mocker.Mock(),
    )

    assert wake.call_count == 1

    machine._cycle()
    assert machine.current_state == machine._BUSY

    # Queue another transaction while busy
    _ = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
        info={"message": "something wonderful is happening..."},
        on_broadcast_failure=mocker.Mock(),
        on_fault=mocker.Mock(),
        on_finalized=mocker.Mock(),
    )

    assert wake.call_count == 1  # remains unchanged


def test_wake_no_call_after_queuing_when_already_paused(
    machine,
    eip1559_transaction,
    account,
    mock_wake_sleep,
    mocker,
):
    wake, sleep = mock_wake_sleep

    assert machine.current_state == machine._IDLE

    machine.pause()
    assert machine.paused
    assert machine.current_state == machine._PAUSED

    assert sleep.call_count == 1

    # Queue a transaction
    _ = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
        info={"message": "something wonderful is happening..."},
        on_broadcast_failure=mocker.Mock(),
        on_fault=mocker.Mock(),
        on_finalized=mocker.Mock(),
    )

    assert wake.call_count == 0


@pytest_twisted.inlineCallbacks
@pytest.mark.usefixtures("disable_auto_mining")
def test_broadcast(
    clock,
    machine,
    state_observer,
    eip1559_transaction,
    account,
    mocker,
    mock_wake_sleep,
):
    wake, _ = mock_wake_sleep

    assert machine.current_state == machine._IDLE
    assert not machine.busy

    # Queue a transaction
    broadcast_hook = mocker.Mock()
    atx = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
        on_broadcast=broadcast_hook,
        on_broadcast_failure=mocker.Mock(),
        on_fault=mocker.Mock(),
        on_finalized=mocker.Mock(),
        info={"message": "something wonderful is happening..."},
    )

    assert wake.call_count == 1

    # There is one queued transaction
    assert len(machine.queued) == 1

    machine.start(now=True)
    while machine.pending is None:
        yield clock.advance(1)

    assert machine.current_state == machine._BUSY

    # The transaction is no longer queued
    assert len(machine.queued) == 0

    assert machine.pending == atx
    assert isinstance(machine.pending, PendingTx)

    assert isinstance(atx, PendingTx)
    assert not atx.final
    assert atx.txhash

    # wait for the hook to be called
    yield deferLater(reactor, 0.2, lambda: None)
    assert broadcast_hook.call_count == 1

    assert atx.retries == 0

    # tx only broadcasted and not finalized, so we are still busy
    assert machine.current_state == machine._BUSY

    assert len(state_observer.transitions) == 1
    assert state_observer.transitions[0] == (machine._IDLE, machine._BUSY)

    machine.stop()


@pytest_twisted.inlineCallbacks
@pytest.mark.usefixtures("disable_auto_mining")
@pytest.mark.parametrize(
    "non_recoverable_error", [ValidationError, ValueError, Web3Exception]
)
def test_broadcast_non_recoverable_error(
    non_recoverable_error,
    clock,
    w3,
    machine,
    state_observer,
    eip1559_transaction,
    account,
    mocker,
    mock_wake_sleep,
):
    wake, _ = mock_wake_sleep

    assert machine.current_state == machine._IDLE
    assert not machine.busy

    # Queue a transaction
    broadcast_hook = mocker.Mock()
    broadcast_failure_hook = mocker.Mock()
    atx = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
        on_broadcast=broadcast_hook,
        on_broadcast_failure=broadcast_failure_hook,
        on_fault=mocker.Mock(),
        on_finalized=mocker.Mock(),
        info={"message": "something wonderful is happening..."},
    )

    assert wake.call_count == 1

    # There is one queued transaction
    assert len(machine.queued) == 1

    # make firing of transaction fail
    error = non_recoverable_error("non-recoverable error")
    assert not _is_recoverable_send_tx_error(error)
    mocker.patch.object(w3.eth, "send_raw_transaction", side_effect=error)

    machine.start(now=True)

    # wait for the hook to be called
    yield deferLater(reactor, 0.2, lambda: None)
    assert broadcast_failure_hook.call_count == 1
    broadcast_failure_hook.assert_called_with(atx, error)

    # tx remains in queue
    assert len(machine.queued) == 1

    # run a few cycles
    for i in range(2):
        yield clock.advance(1)

    assert broadcast_hook.call_count == 0

    assert atx.retries == 0

    # tx failed and not requeued
    assert machine.current_state == machine._BUSY

    assert len(state_observer.transitions) == 1
    assert state_observer.transitions[0] == (machine._IDLE, machine._BUSY)

    machine.stop()


@pytest_twisted.inlineCallbacks
@pytest.mark.usefixtures("disable_auto_mining")
@pytest.mark.parametrize(
    "recoverable_error", [TooManyRequests, ProviderConnectionError, TimeExhausted]
)
def test_broadcast_recoverable_error(
    recoverable_error,
    clock,
    w3,
    machine,
    state_observer,
    eip1559_transaction,
    account,
    mocker,
    mock_wake_sleep,
):
    # need more freedom with redo attempts for test
    mocker.patch.object(machine, "_MAX_RETRY_ATTEMPTS", 10)

    wake, _ = mock_wake_sleep

    assert machine.current_state == machine._IDLE
    assert not machine.busy

    # Queue a transaction
    broadcast_hook = mocker.Mock()
    broadcast_failure_hook = mocker.Mock()
    atx = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
        on_broadcast=broadcast_hook,
        on_broadcast_failure=broadcast_failure_hook,
        on_fault=mocker.Mock(),
        on_finalized=mocker.Mock(),
        info={"message": "something wonderful is happening..."},
    )

    assert wake.call_count == 1

    # There is one queued transaction
    assert len(machine.queued) == 1

    real_method = w3.eth.send_raw_transaction
    # make firing of transaction fail but with recoverable error
    error = recoverable_error("recoverable error")
    assert _is_recoverable_send_tx_error(error)
    mocker.patch.object(w3.eth, "send_raw_transaction", side_effect=error)

    # repeat some cycles; tx fails then gets requeued since error is "recoverable"
    machine.start(now=True)
    for i in range(5):
        yield clock.advance(1)
        assert len(machine.queued) == 1  # remains in queue and not broadcasted
        assert atx.retries >= i

    # call real method from now on
    mocker.patch.object(w3.eth, "send_raw_transaction", side_effect=real_method)
    while machine.pending is None:
        yield clock.advance(1)

    assert machine.current_state == machine._BUSY

    # The transaction is broadcasted and no longer queued
    assert len(machine.queued) == 0

    assert machine.pending == atx
    assert isinstance(machine.pending, PendingTx)

    assert isinstance(atx, PendingTx)
    assert not atx.final
    assert atx.txhash

    # wait for the hook to be called
    yield deferLater(reactor, 0.2, lambda: None)
    assert broadcast_hook.call_count == 1
    assert broadcast_failure_hook.call_count == 0

    # tx only broadcasted and not finalized, so we are still busy
    assert machine.current_state == machine._BUSY

    assert len(state_observer.transitions) == 1
    assert state_observer.transitions[0] == (machine._IDLE, machine._BUSY)

    machine.stop()


@pytest_twisted.inlineCallbacks
@pytest.mark.usefixtures("disable_auto_mining")
@pytest.mark.parametrize(
    "recoverable_error", [TooManyRequests, ProviderConnectionError, TimeExhausted]
)
def test_broadcast_recoverable_error_retries_exceeded(
    recoverable_error,
    clock,
    w3,
    machine,
    state_observer,
    eip1559_transaction,
    account,
    mocker,
    mock_wake_sleep,
):
    wake, _ = mock_wake_sleep

    assert machine.current_state == machine._IDLE
    assert not machine.busy

    # Queue a transaction
    broadcast_hook = mocker.Mock()
    broadcast_failure_hook = mocker.Mock()
    atx = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
        on_broadcast=broadcast_hook,
        on_broadcast_failure=broadcast_failure_hook,
        on_fault=mocker.Mock(),
        on_finalized=mocker.Mock(),
        info={"message": "something wonderful is happening..."},
    )

    assert wake.call_count == 1

    # There is one queued transaction
    assert len(machine.queued) == 1

    # make firing of transaction fail but with recoverable error
    error = recoverable_error("recoverable error")
    assert _is_recoverable_send_tx_error(error)
    mocker.patch.object(w3.eth, "send_raw_transaction", side_effect=error)

    # repeat some cycles; tx fails then gets retried since error is "recoverable"
    machine.start(now=True)
    # one less than max attempts
    for i in range(machine._MAX_RETRY_ATTEMPTS - 1):
        assert len(machine.queued) == 1  # remains in queue and not broadcasted
        yield clock.advance(1)
        assert atx.retries >= i

    # push over the retry limit
    yield clock.advance(1)

    # wait for the hook to be called
    yield deferLater(reactor, 0.2, lambda: None)
    assert broadcast_failure_hook.call_count == 1
    broadcast_failure_hook.assert_called_with(atx, error)

    # The transaction failed but remains in the queue, unless the user does something
    assert len(machine.queued) == 1

    # retries are reset
    assert atx.retries == 0

    # run a few cycles
    for i in range(machine._MAX_RETRY_ATTEMPTS - 1):
        yield clock.advance(1)

    assert broadcast_hook.call_count == 0

    assert len(state_observer.transitions) == 1
    assert state_observer.transitions[0] == (machine._IDLE, machine._BUSY)

    machine.stop()


@pytest_twisted.inlineCallbacks
@pytest.mark.usefixtures("disable_auto_mining")
def test_finalize(
    ethereum_tester,
    clock,
    machine,
    state_observer,
    eip1559_transaction,
    account,
    mock_wake_sleep,
    mocker,
):
    assert machine.current_state == machine._IDLE

    # Queue a transaction
    finalized_hook = mocker.Mock()
    atx = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
        on_finalized=finalized_hook,
        on_broadcast_failure=mocker.Mock(),
        on_fault=mocker.Mock(),
    )

    # There is one queued transaction
    assert len(machine.queued) == 1

    machine.start(now=True)

    # advance to broadcast the transaction
    while machine.pending is None:
        yield clock.advance(1)

    assert machine.current_state == machine._BUSY

    assert machine.pending == atx

    # advance to finalize the transaction
    while machine.pending:
        yield ethereum_tester.mine_block()
        yield clock.advance(1)

    # The transaction is no longer pending
    assert machine.pending is None

    assert machine.current_state == machine._BUSY

    # The transaction is tracked as finalized
    assert len(machine.finalized) == 1

    # async transaction reflects finalized state
    assert atx.final
    assert atx.receipt
    assert atx.successful

    # wait for the hook to be called
    yield deferLater(reactor, 0.2, lambda: None)
    assert finalized_hook.call_count == 1
    finalized_hook.assert_called_with(atx)

    yield clock.advance(1)

    assert machine.current_state == machine._IDLE

    assert len(state_observer.transitions) == 2
    assert state_observer.transitions[0] == (machine._IDLE, machine._BUSY)
    assert state_observer.transitions[1] == (machine._BUSY, machine._IDLE)

    machine.stop()


@pytest_twisted.inlineCallbacks
@pytest.mark.usefixtures("disable_auto_mining")
def test_follow(
    ethereum_tester,
    machine,
    state_observer,
    clock,
    eip1559_transaction,
    account,
    mock_wake_sleep,
    mocker,
):
    machine.start()
    assert machine.current_state == machine._IDLE

    atx = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
        on_broadcast_failure=mocker.Mock(),
        on_fault=mocker.Mock(),
        on_finalized=mocker.Mock(),
    )

    # advance to broadcast the transaction
    while machine.pending is None:
        yield clock.advance(1)

    assert machine.current_state == machine._BUSY

    while not machine.finalized:
        yield ethereum_tester.mine_block()
        yield clock.advance(1)

    assert atx.final is True

    while len(machine.finalized) > 0:
        yield ethereum_tester.mine_block()
        yield clock.advance(1)

    assert len(machine.finalized) == 0
    assert len(machine.queued) == 0
    assert machine.pending is None

    assert not machine.busy

    assert machine.current_state == machine._IDLE

    assert len(state_observer.transitions) == 2
    assert state_observer.transitions[0] == (machine._IDLE, machine._BUSY)
    assert state_observer.transitions[1] == (machine._BUSY, machine._IDLE)

    machine.stop()


@pytest_twisted.inlineCallbacks
@pytest.mark.usefixtures("disable_auto_mining")
def test_use_strategies_speedup_used(
    ethereum_tester,
    machine,
    state_observer,
    clock,
    eip1559_transaction,
    account,
    mocker,
    mock_wake_sleep,
    strategies,
):
    machine.start()
    assert machine.current_state == machine._IDLE

    old_max_fee = eip1559_transaction["maxFeePerGas"]
    old_priority_fee = eip1559_transaction["maxPriorityFeePerGas"]

    broadcast_hook = mocker.Mock()
    atx = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
        on_broadcast=broadcast_hook,
        on_broadcast_failure=mocker.Mock(),
        on_fault=mocker.Mock(),
        on_finalized=mocker.Mock(),
    )

    update_spy = mocker.spy(
        machine._tx_tracker, "update_active_after_successful_strategy_update"
    )

    # advance to broadcast the transaction
    while machine.pending is None:
        yield clock.advance(1)

    # ensure that hook is called
    yield deferLater(reactor, 0.2, lambda: None)
    assert broadcast_hook.call_count == 1
    broadcast_hook.assert_called_with(atx), "called with correct parameter"

    assert machine.current_state == machine._BUSY
    original_params = dict(atx.params)
    atx.last_updated = int(time.time() - strategies[0].min_time_between_speedups - 1)

    # need some cycles while tx unmined for strategies to kick in
    for i in range(2):
        yield clock.advance(1)

    # ensure that hook is called
    yield deferLater(reactor, 0.2, lambda: None)
    assert (
        broadcast_hook.call_count > 1
    ), "additional calls to broadcast since tx retried/replaced"
    broadcast_hook.assert_called_with(atx), "called with correct parameter"

    assert atx.params != original_params, "params changed"
    assert update_spy.call_count > 0, "params updated"

    while not machine.finalized:
        yield ethereum_tester.mine_block()
        yield clock.advance(1)

    assert atx.final is True

    # speed up strategy kicked in
    assert atx.params["maxFeePerGas"] > old_max_fee
    assert atx.params["maxPriorityFeePerGas"] > old_priority_fee

    while len(machine.finalized) > 0:
        yield ethereum_tester.mine_block()
        yield clock.advance(1)

    assert len(machine.finalized) == 0
    assert len(machine.queued) == 0
    assert machine.pending is None

    assert not machine.busy

    assert machine.current_state == machine._IDLE

    assert len(state_observer.transitions) == 2
    assert state_observer.transitions[0] == (machine._IDLE, machine._BUSY)
    assert state_observer.transitions[1] == (machine._BUSY, machine._IDLE)

    machine.stop()


@pytest_twisted.inlineCallbacks
@pytest.mark.usefixtures("disable_auto_mining")
def test_use_strategies_timeout_used(
    ethereum_tester,
    w3,
    machine,
    state_observer,
    clock,
    eip1559_transaction,
    account,
    mocker,
    mock_wake_sleep,
):
    fault_hook = mocker.Mock()

    machine.start()
    assert machine.current_state == machine._IDLE

    atx = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
        on_broadcast_failure=mocker.Mock(),
        on_fault=fault_hook,
        on_finalized=mocker.Mock(),
    )

    # advance to broadcast the transaction
    while machine.pending is None:
        yield clock.advance(1)

    assert machine.current_state == machine._BUSY

    # don't mine transaction at all; wait for hook to be called when timeout occurs

    # need some cycles while tx unmined for strategies to kick in
    num_cycles = 4
    for i in range(num_cycles):
        # reduce creation time by timeout to force timeout
        atx.created -= math.ceil(TimeoutStrategy.TIMEOUT / (num_cycles - 1))
        yield clock.advance(1)

    # ensure switch back to IDLE
    yield clock.advance(1)

    # ensure that hook is called
    yield deferLater(reactor, 0.2, lambda: None)

    assert fault_hook.call_count == 1
    fault_hook.assert_called_with(atx)

    assert len(machine.queued) == 0
    assert machine.pending is None

    assert not machine.busy
    assert not atx.final

    assert machine.current_state == machine._IDLE

    assert len(state_observer.transitions) == 2
    assert state_observer.transitions[0] == (machine._IDLE, machine._BUSY)
    assert state_observer.transitions[1] == (machine._BUSY, machine._IDLE)

    machine.stop()


@pytest_twisted.inlineCallbacks
@pytest.mark.usefixtures("disable_auto_mining")
def test_use_strategies_that_dont_make_updates(
    ethereum_tester,
    w3,
    machine,
    state_observer,
    clock,
    eip1559_transaction,
    account,
    mocker,
    mock_wake_sleep,
):
    # strategies that don't make updates
    strategy_1 = mocker.Mock(spec=AsyncTxStrategy)
    strategy_1.execute.return_value = None
    strategy_2 = mocker.Mock(spec=AsyncTxStrategy)
    strategy_2.execute.return_value = None

    _configure_machine_strategies(machine, [strategy_1, strategy_2])

    update_spy = mocker.spy(
        machine._tx_tracker, "update_active_after_successful_strategy_update"
    )

    machine.start()
    assert machine.current_state == machine._IDLE

    broadcast_hook = mocker.Mock()
    atx = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
        on_broadcast=broadcast_hook,
        on_broadcast_failure=mocker.Mock(),
        on_fault=mocker.Mock(),
        on_finalized=mocker.Mock(),
    )

    # advance to broadcast the transaction
    while machine.pending is None:
        yield clock.advance(1)

    # ensure that hook is called
    yield deferLater(reactor, 0.2, lambda: None)
    assert broadcast_hook.call_count == 1
    broadcast_hook.assert_called_with(atx), "called with correct parameter"

    original_params = dict(atx.params)

    assert machine.current_state == machine._BUSY

    # need some cycles while tx unmined for strategies to kick in
    num_cycles = 4
    for i in range(num_cycles):
        yield clock.advance(1)
        # params remained unchanged since strategies don't make updates
        assert atx.params == original_params, "params remain unchanged"

    assert strategy_1.execute.call_count > 0, "strategy #1 was called"
    assert strategy_2.execute.call_count > 0, "strategy #2 was called"

    assert atx.params == original_params, "params remain unchanged"
    assert update_spy.call_count == 0, "update never called because no retry"

    # mine tx
    ethereum_tester.mine_block()
    yield clock.advance(1)

    # ensure switch back to IDLE
    yield clock.advance(1)

    assert len(machine.queued) == 0
    assert machine.pending is None

    assert not machine.busy
    assert atx.final

    assert machine.current_state == machine._IDLE

    assert len(state_observer.transitions) == 2
    assert state_observer.transitions[0] == (machine._IDLE, machine._BUSY)
    assert state_observer.transitions[1] == (machine._BUSY, machine._IDLE)

    machine.stop()


@pytest_twisted.inlineCallbacks
@pytest.mark.usefixtures("disable_auto_mining")
@pytest.mark.parametrize(
    "retry_error",
    [
        ValidationError,
        ValueError,
        Web3Exception,
        TooManyRequests,
        ProviderConnectionError,
        TimeExhausted,
    ],
)
def test_retry_with_errors_but_recovers(
    retry_error,
    ethereum_tester,
    w3,
    machine,
    state_observer,
    clock,
    eip1559_transaction,
    account,
    mocker,
    mock_wake_sleep,
):
    # need more freedom with redo attempts for test
    mocker.patch.object(machine, "_MAX_RETRY_ATTEMPTS", 10)

    # strategies that don't make updates
    strategy_1 = mocker.Mock(spec=AsyncTxStrategy)
    strategy_1.name = "mock_strategy"
    # return non-None so retry is attempted
    strategy_1.execute.return_value = dict(eip1559_transaction)

    _configure_machine_strategies(machine, [strategy_1])

    update_spy = mocker.spy(
        machine._tx_tracker, "update_active_after_successful_strategy_update"
    )

    machine.start()
    assert machine.current_state == machine._IDLE

    broadcast_hook = mocker.Mock()
    fault_hook = mocker.Mock()
    atx = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
        on_fault=fault_hook,
        on_broadcast=broadcast_hook,
        on_broadcast_failure=mocker.Mock(),
        on_finalized=mocker.Mock(),
    )

    # advance to broadcast the transaction
    while machine.pending is None:
        yield clock.advance(1)

    # ensure that hook is called
    yield deferLater(reactor, 0.2, lambda: None)
    assert broadcast_hook.call_count == 1

    assert machine.current_state == machine._BUSY

    real_method = w3.eth.send_raw_transaction

    # make firing of retry transaction fail with non-recoverable error
    error = retry_error("retry error")
    mocker.patch.object(w3.eth, "send_raw_transaction", side_effect=error)

    # run a cycle while tx unmined for strategies to kick in
    for i in range(5):
        yield clock.advance(1)
        assert (
            machine.pending
        ), "tx is being retried but encounters retry error and remains pending"
        assert atx.retries >= i

    assert strategy_1.execute.call_count > 0, "strategy #1 was called"
    # retries failed, so params shouldn't have been updated
    assert update_spy.call_count == 0, "update never called because each retry failed"

    # call real method from now on
    mocker.patch.object(w3.eth, "send_raw_transaction", side_effect=real_method)

    while not machine.finalized:
        yield ethereum_tester.mine_block()
        yield clock.advance(1)

    yield clock.advance(1)
    assert atx.final is True

    # ensure that hook is not called
    yield deferLater(reactor, 0.2, lambda: None)
    assert fault_hook.call_count == 0

    # ensure switch back to IDLE
    yield clock.advance(1)

    assert len(machine.queued) == 0
    assert machine.pending is None

    assert not machine.busy
    assert atx.final
    assert isinstance(atx, FinalizedTx)

    assert machine.current_state == machine._IDLE

    assert len(state_observer.transitions) == 2
    assert state_observer.transitions[0] == (machine._IDLE, machine._BUSY)
    assert state_observer.transitions[1] == (machine._BUSY, machine._IDLE)

    machine.stop()


@pytest_twisted.inlineCallbacks
@pytest.mark.usefixtures("disable_auto_mining")
@pytest.mark.parametrize(
    "retry_error",
    [
        ValidationError,
        ValueError,
        Web3Exception,
        TooManyRequests,
        ProviderConnectionError,
        TimeExhausted,
    ],
)
def test_retry_with_errors_retries_exceeded(
    retry_error,
    ethereum_tester,
    w3,
    machine,
    state_observer,
    clock,
    eip1559_transaction,
    account,
    mocker,
    mock_wake_sleep,
):
    # strategies that don't make updates
    strategy_1 = mocker.Mock(spec=AsyncTxStrategy)
    strategy_1.name = "mock_strategy"
    # return non-None so retry is attempted
    strategy_1.execute.return_value = dict(eip1559_transaction)

    _configure_machine_strategies(machine, [strategy_1])

    update_spy = mocker.spy(
        machine._tx_tracker, "update_active_after_successful_strategy_update"
    )

    machine.start()
    assert machine.current_state == machine._IDLE

    broadcast_hook = mocker.Mock()
    fault_hook = mocker.Mock()
    atx = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
        on_broadcast_failure=mocker.Mock(),
        on_fault=fault_hook,
        on_broadcast=broadcast_hook,
        on_finalized=mocker.Mock(),
    )

    # advance to broadcast the transaction
    while machine.pending is None:
        yield clock.advance(1)

    # ensure that hook is called
    yield deferLater(reactor, 0.2, lambda: None)
    assert broadcast_hook.call_count == 1

    assert machine.current_state == machine._BUSY

    # make firing of retry transaction fail with non-recoverable error
    error = retry_error("retry error")
    mocker.patch.object(w3.eth, "send_raw_transaction", side_effect=error)

    # retry max attempts
    for i in range(machine._MAX_RETRY_ATTEMPTS):
        assert machine.pending is not None
        yield clock.advance(1)
        assert atx.retries >= i

    # push over retry limit
    yield clock.advance(1)

    assert strategy_1.execute.call_count > 0, "strategy #1 was called"
    # retries failed, so params shouldn't have been updated
    assert update_spy.call_count == 0, "update never called because each retry failed"

    # wait for the hook to be called
    yield deferLater(reactor, 0.2, lambda: None)
    assert fault_hook.call_count == 1
    fault_hook.assert_called_with(atx)

    assert atx.retries == machine._MAX_RETRY_ATTEMPTS

    assert len(machine.queued) == 0
    assert atx.final is False
    assert isinstance(atx, FaultedTx)

    # ensure switch back to IDLE
    yield clock.advance(1)

    assert len(machine.queued) == 0
    assert machine.pending is None

    assert not machine.busy

    assert machine.current_state == machine._IDLE

    assert len(state_observer.transitions) == 2
    assert state_observer.transitions[0] == (machine._IDLE, machine._BUSY)
    assert state_observer.transitions[1] == (machine._BUSY, machine._IDLE)

    machine.stop()


@pytest_twisted.inlineCallbacks
def test_pause_when_idle(clock, machine, mocker):
    machine.start()
    assert machine.current_state == machine._IDLE
    assert machine.running

    stop_spy = mocker.spy(machine._task, "stop")
    start_spy = mocker.spy(machine._task, "start")

    machine.pause()
    yield clock.advance(1)

    assert machine.current_state == machine._PAUSED

    assert not machine.running

    assert stop_spy.call_count == 1, "task stopped since paused"
    assert start_spy.call_count == 0

    machine.resume()

    assert stop_spy.call_count == 1
    assert start_spy.call_count == 1, "machine restarted"

    assert machine.current_state == machine._IDLE
    assert machine.running
    machine.stop()


@pytest_twisted.inlineCallbacks
def test_pause_when_busy(clock, machine, eip1559_transaction, account, mocker):
    machine.start()
    assert machine.current_state == machine._IDLE
    assert machine.running

    _ = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
        on_broadcast_failure=mocker.Mock(),
        on_fault=mocker.Mock(),
        on_finalized=mocker.Mock(),
    )

    # advance to broadcast the transaction
    while machine.pending is None:
        yield clock.advance(1)

    assert machine.current_state == machine._BUSY

    stop_spy = mocker.spy(machine._task, "stop")
    start_spy = mocker.spy(machine._task, "start")

    machine.pause()
    yield clock.advance(1)

    assert machine.current_state == machine._PAUSED

    assert not machine.running

    assert stop_spy.call_count == 1, "task stopped since paused"
    assert start_spy.call_count == 0

    machine.resume()

    assert stop_spy.call_count == 1
    assert start_spy.call_count == 1, "machine restarted"

    assert machine.current_state == machine._BUSY
    assert machine.running
    machine.stop()


@pytest.mark.usefixtures("disable_auto_mining")
def test_simple_state_transitions(
    ethereum_tester,
    machine,
    eip1559_transaction,
    account,
    mock_wake_sleep,
    mocker,
):
    assert machine.current_state == machine._IDLE

    for i in range(3):
        machine._cycle()
        # no change in state
        assert machine.current_state == machine._IDLE

    # idle -> pause
    machine.pause()
    assert machine.current_state == machine._PAUSED
    assert machine.paused

    # calling pause has no effect if already paused
    assert machine.paused
    for i in range(3):
        machine.pause()

    # resume after pausing
    machine.resume()
    machine._cycle()  # wake doesn't do anything because mocked
    assert machine.current_state == machine._IDLE
    assert not machine.paused
    assert not machine.busy

    atx = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
        on_broadcast_failure=mocker.Mock(),
        on_fault=mocker.Mock(),
        on_finalized=mocker.Mock(),
    )

    # broadcast tx
    machine._cycle()
    assert machine.current_state == machine._BUSY
    assert machine.busy

    ethereum_tester.mine_block()

    # busy -> pause
    machine.pause()
    assert machine.current_state == machine._PAUSED
    assert machine.paused

    # resume after pausing
    machine.resume()
    machine._cycle()  # wake doesn't do anything because mocked
    assert machine.current_state == machine._BUSY
    assert not machine.paused

    # finalize tx
    while machine.busy:
        ethereum_tester.mine_block()
        machine._cycle()

    ethereum_tester.mine_block()
    assert atx.final is True

    # transition to idle
    machine._cycle()
    assert machine.current_state == machine._IDLE

    # resume has no effect if not paused
    wake, sleep = mock_wake_sleep
    wake_call_count = wake.call_count
    sleep_call_count = sleep.call_count

    assert not machine.paused
    for i in range(3):
        machine.resume()
        assert machine.current_state == machine._IDLE
        assert wake.call_count == wake_call_count, "wake call count remains unchanged"
        assert sleep.call_count == sleep_call_count, "wake call count remains unchanged"


def _configure_machine_strategies(
    machine: AutomaticTxMachine, strategies: List[AsyncTxStrategy]
):
    # create updated strategies list with the default TimeoutStrategy
    machine._strategies.clear()
    machine._strategies.append(TimeoutStrategy(machine.w3))
    machine._strategies.extend(strategies)

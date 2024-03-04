import pytest

import pytest_twisted
from twisted.internet import reactor
from twisted.internet.task import deferLater

from atxm.tx import FutureTx, PendingTx


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


def test_queue(
    machine,
    state_observer,
    rpc_spy,
    account,
    eip1559_transaction,
    mock_wake_sleep,
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
):
    wake, _ = mock_wake_sleep

    assert machine.current_state == machine._IDLE

    # Queue a transaction
    _ = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
        info={"message": "something wonderful is happening..."},
    )

    assert wake.call_count == 1

    machine._cycle()
    assert machine.current_state == machine._BUSY

    # Queue another transaction while busy
    _ = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
        info={"message": "something wonderful is happening..."},
    )

    assert wake.call_count == 1  # remains unchanged


def test_wake_no_call_after_queuing_when_already_paused(
    machine,
    eip1559_transaction,
    account,
    mock_wake_sleep,
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
    )

    assert wake.call_count == 0


@pytest_twisted.inlineCallbacks
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
    hook = mocker.Mock()
    atx = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
        on_broadcast=hook,
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
    assert hook.call_count == 1

    # tx only broadcasted and not finalized, so we are still busy
    assert machine.current_state == machine._BUSY

    assert len(state_observer.transitions) == 1
    assert state_observer.transitions[0] == (machine._IDLE, machine._BUSY)

    machine.stop()


@pytest_twisted.inlineCallbacks
def test_finalize(
    chain,
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
    hook = mocker.Mock()
    atx = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
        on_finalized=hook,
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
        yield chain.mine(1)
        yield clock.advance(1)

    # The transaction is no longer pending
    assert machine.pending is None

    assert machine.current_state == machine._BUSY

    # The transaction is tracked as finalized
    assert len(machine.finalized) == 1

    # async transaction reflects finalized state
    assert atx.final
    assert atx.receipt

    # wait for the hook to be called
    yield deferLater(reactor, 0.2, lambda: None)
    assert hook.call_count == 1

    yield clock.advance(1)

    assert machine.current_state == machine._IDLE

    assert len(state_observer.transitions) == 2
    assert state_observer.transitions[0] == (machine._IDLE, machine._BUSY)
    assert state_observer.transitions[1] == (machine._BUSY, machine._IDLE)

    machine.stop()


@pytest_twisted.inlineCallbacks
def test_follow(
    chain, machine, state_observer, clock, eip1559_transaction, account, mock_wake_sleep
):
    machine.start()
    assert machine.current_state == machine._IDLE

    atx = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
    )

    # advance to broadcast the transaction
    while machine.pending is None:
        yield clock.advance(1)

    assert machine.current_state == machine._BUSY

    while not machine.finalized:
        yield clock.advance(1)

    assert atx.final is True

    while len(machine.finalized) > 0:
        yield chain.mine(1)
        yield clock.advance(1)

    assert len(machine.finalized) == 0
    assert len(machine.queued) == 0
    assert len(machine.faults) == 0
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


def test_simple_state_transitions(
    chain, machine, eip1559_transaction, account, mock_wake_sleep
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
    )

    # broadcast tx
    machine._cycle()
    assert machine.current_state == machine._BUSY
    assert machine.busy

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
        chain.mine(1)
        machine._cycle()

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

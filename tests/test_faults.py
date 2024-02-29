import pytest
import pytest_twisted
from twisted.internet import reactor
from twisted.internet.task import deferLater
from web3.exceptions import TransactionNotFound

from atxm.exceptions import Fault
from atxm.tx import FaultedTx


@pytest.fixture
def mock_eth_get_transaction(mocker, w3):
    return mocker.patch.object(
        w3.eth,
        "get_transaction",
        side_effect=TransactionNotFound
    )


@pytest_twisted.inlineCallbacks
def test_timeout(
        machine, clock, eip1559_transaction, account, interval,
        mock_wake_sleep, mocker, mock_eth_get_transaction
):
    hook = mocker.Mock()
    atx = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
        on_fault=hook,
    )

    machine.start()
    while not machine.pending:
        yield clock.advance(interval)
    machine.stop()
    assert not machine.running

    assert machine.pending == atx
    assert atx.final is False
    assert atx.fault is None

    atx.created -= 9999999999
    machine.start()
    while machine.pending:
        yield clock.advance(interval)
    machine.stop()

    assert atx.final is False
    assert isinstance(atx.fault, Fault)
    assert isinstance(atx, FaultedTx)

    # check async tx advanced through the state machine
    assert atx not in machine.queued
    assert machine.pending is None
    assert atx.final is False

    yield deferLater(reactor, 0.2, lambda: None)
    assert hook.call_count == 1

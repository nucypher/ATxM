import pytest_twisted
from twisted.internet import reactor
from twisted.internet.task import deferLater
from web3.exceptions import TransactionNotFound
from web3.types import TxReceipt

from atxm.exceptions import Fault, TransactionFaulted
from atxm.strategies import AsyncTxStrategy
from atxm.tx import FaultedTx
from atxm.utils import _get_receipt_from_txhash


def _broadcast_tx(machine, eip1559_transaction, account, mocker):
    fault_hook = mocker.Mock()

    atx = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
        on_fault=fault_hook,
    )

    # broadcast tx
    machine._cycle()

    assert machine.pending == atx
    assert atx.final is False
    assert atx.fault is None

    return atx, fault_hook


@pytest_twisted.inlineCallbacks
def _verify_tx_faulted(machine, atx, fault_hook, expected_fault: Fault):
    machine._cycle()

    # ensure hook is called
    yield deferLater(reactor, 0.2, lambda: None)
    assert fault_hook.call_count == 1
    fault_hook.assert_called_with(atx)

    assert atx.final is False
    assert isinstance(atx, FaultedTx)
    assert isinstance(atx.fault, Fault)
    assert atx.fault == expected_fault

    # check async tx advanced through the state machine
    assert atx not in machine.queued

    assert machine.pending is None
    assert atx.final is False


def test_revert(
    chain,
    w3,
    machine,
    clock,
    eip1559_transaction,
    account,
    interval,
    mock_wake_sleep,
    mocker,
):
    atx, fault_hook = _broadcast_tx(machine, eip1559_transaction, account, mocker)

    assert machine.pending

    chain.mine(1)

    # force receipt to symbolize a revert of the tx
    receipt = _get_receipt_from_txhash(w3, atx.txhash)
    revert_receipt = dict(receipt)
    revert_receipt["status"] = 0

    mocker.patch.object(
        w3.eth, "get_transaction_receipt", return_value=TxReceipt(revert_receipt)
    )

    _verify_tx_faulted(machine, atx, fault_hook, expected_fault=Fault.REVERT)


def test_strategy_fault(
    w3, machine, clock, eip1559_transaction, account, interval, mock_wake_sleep, mocker
):
    faulty_strategy = mocker.Mock(spec=AsyncTxStrategy)

    # TODO: consider whether strategies should just be overriden through the constructor
    machine._strategies.insert(0, faulty_strategy)  # add first

    atx, fault_hook = _broadcast_tx(machine, eip1559_transaction, account, mocker)

    faulty_message = "mocked fault"
    faulty_strategy.execute.side_effect = TransactionFaulted(
        tx=atx, fault=Fault.ERROR, message=faulty_message
    )

    mocker.patch.object(
        w3.eth, "get_transaction_receipt", side_effect=TransactionNotFound
    )

    _verify_tx_faulted(machine, atx, fault_hook, expected_fault=Fault.ERROR)
    assert atx.error == faulty_message


def test_timeout_strategy_fault(
    w3, machine, clock, eip1559_transaction, account, interval, mock_wake_sleep, mocker
):
    atx, fault_hook = _broadcast_tx(machine, eip1559_transaction, account, mocker)

    atx.created -= 9999999999
    mocker.patch.object(w3.eth, "get_transaction", side_effect=TransactionNotFound)

    _verify_tx_faulted(machine, atx, fault_hook, expected_fault=Fault.TIMEOUT)

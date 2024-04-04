import pytest
import pytest_twisted
from twisted.internet import reactor
from twisted.internet.task import deferLater

from atxm.exceptions import Fault, TransactionFaulted
from atxm.strategies import AsyncTxStrategy
from atxm.tx import FaultedTx


def _broadcast_tx(machine, eip1559_transaction, account, mocker):
    fault_hook = mocker.Mock()

    atx = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
        on_broadcast_failure=mocker.Mock(),
        on_fault=fault_hook,
        on_finalized=mocker.Mock(),
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


@pytest.mark.usefixtures("disable_auto_mining")
def test_strategy_fault(
    w3,
    machine,
    clock,
    eip1559_transaction,
    account,
    interval,
    mock_wake_sleep,
    mocker,
):
    faulty_strategy = mocker.Mock(spec=AsyncTxStrategy)

    # TODO: consider whether strategies should just be overridden through the constructor
    machine._strategies.insert(0, faulty_strategy)  # add first

    atx, fault_hook = _broadcast_tx(machine, eip1559_transaction, account, mocker)

    faulty_message = "mocked fault"
    faulty_strategy.execute.side_effect = TransactionFaulted(
        tx=atx, fault=Fault.ERROR, message=faulty_message
    )

    # tx not mined
    _verify_tx_faulted(machine, atx, fault_hook, expected_fault=Fault.ERROR)
    assert atx.error == faulty_message


@pytest.mark.usefixtures("disable_auto_mining")
def test_timeout_strategy_fault(
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

    atx.created -= 9999999999

    # tx not mined
    _verify_tx_faulted(machine, atx, fault_hook, expected_fault=Fault.TIMEOUT)

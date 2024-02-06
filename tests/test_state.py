# TODO: check for clearance of tx hash on ritual tracker
#
#     # pending tx gets mined and removed from storage - receipt status is 1
#     mock_receipt = {"status": 1}
#     with patch.object(
#         agent.blockchain.client, "get_transaction_receipt", return_value=mock_receipt
#     ):
#         tx_hash = ursula.perform_round_1(
#             ritual_id=0, authority=random_address, participants=cohort, timestamp=0
#         )
#         # no execution since pending tx was present and determined to be mined
#         assert tx_hash is None
#         # tx hash removed since tx receipt was obtained - outcome moving
#         # forward is represented on contract
#         assert ursula.dkg_storage.get_transcript_txhash(ritual_id=0) is None
#
#     # reset tx hash
#     ursula.dkg_storage.store_transcript_txhash(ritual_id=0, txhash=original_tx_hash)
#
#     # pending tx gets mined and removed from storage - receipt
#     # status is 0 i.e. evm revert - so use contract state which indicates
#     # to submit transcript
#     mock_receipt = {"status": 0}
#     with patch.object(
#         agent.blockchain.client, "get_transaction_receipt", return_value=mock_receipt
#     ):
#         with patch.object(
#             agent, "post_transcript", lambda *args, **kwargs: HexBytes("A1B1")
#         ):
#             mock_tx_hash = ursula.perform_round_1(
#                 ritual_id=0, authority=random_address, participants=cohort, timestamp=0
#             )
#             # execution occurs because evm revert causes execution to be retried
#             assert mock_tx_hash == HexBytes("A1B1")
#             # tx hash changed since original tx hash removed due to status being 0
#             # and new tx hash added
#             # forward is represented on contract
#             assert ursula.dkg_storage.get_transcript_txhash(ritual_id=0) == mock_tx_hash
#             assert (
#                 ursula.dkg_storage.get_transcript_txhash(ritual_id=0)
#                 != original_tx_hash
#             )
#
#     # reset tx hash
#     ursula.dkg_storage.store_transcript_txhash(ritual_id=0, txhash=original_tx_hash)
#
#     # don't clear if tx hash mismatched
#     assert ursula.dkg_storage.get_transcript_txhash(ritual_id=0) is not None
#     assert not ursula.dkg_storage.clear_transcript_txhash(
#         ritual_id=0, txhash=HexBytes("abcd")
#     )
#     assert ursula.dkg_storage.get_transcript_txhash(ritual_id=0) is not None
# =======


# TODO: check for clearance of tx hash on ritual tracker
#
#     # pending tx gets mined and removed from storage - receipt status is 1
#     mock_receipt = {"status": 1}
#     with patch.object(
#         agent.blockchain.client, "get_transaction_receipt", return_value=mock_receipt
#     ):
#         tx_hash = ursula.perform_round_2(ritual_id=0, timestamp=0)
#         # no execution since pending tx was present and determined to be mined
#         assert tx_hash is None
#         # tx hash removed since tx receipt was obtained - outcome moving
#         # forward is represented on contract
#         assert ursula.dkg_storage.get_aggregation_txhash(ritual_id=0) is None
#
#     # reset tx hash
#     ursula.dkg_storage.store_aggregation_txhash(ritual_id=0, txhash=original_tx_hash)
#
#     # pending tx gets mined and removed from storage - receipt
#     # status is 0 i.e. evm revert - so use contract state which indicates
#     # to submit transcript
#     mock_receipt = {"status": 0}
#     with patch.object(
#         agent.blockchain.client, "get_transaction_receipt", return_value=mock_receipt
#     ):
#         with patch.object(
#             agent, "post_aggregation", lambda *args, **kwargs: HexBytes("A1B1")
#         ):
#             mock_tx_hash = ursula.perform_round_2(ritual_id=0, timestamp=0)
#             # execution occurs because evm revert causes execution to be retried
#             assert mock_tx_hash == HexBytes("A1B1")
#             # tx hash changed since original tx hash removed due to status being 0
#             # and new tx hash added
#             # forward is represented on contract
#             assert (
#                 ursula.dkg_storage.get_aggregation_txhash(ritual_id=0) == mock_tx_hash
#             )
#             assert (
#                 ursula.dkg_storage.get_aggregation_txhash(ritual_id=0)
#                 != original_tx_hash
#             )
#
#     # reset tx hash
#     ursula.dkg_storage.store_aggregation_txhash(ritual_id=0, txhash=original_tx_hash)
#
#     # don't clear if tx hash mismatched
#     assert not ursula.dkg_storage.clear_aggregated_txhash(
#         ritual_id=0, txhash=HexBytes("1234")
#     )
#     assert ursula.dkg_storage.get_aggregation_txhash(ritual_id=0) is not None

    # participant already posted aggregated transcript
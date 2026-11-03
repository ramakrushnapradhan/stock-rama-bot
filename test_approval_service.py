import unittest
from fastapi import HTTPException

from approval_service import TradeProposal, validate_proposal


class ApprovalValidationTests(unittest.TestCase):
    def proposal(self, **overrides):
        values = {
            "symbol": "INFY",
            "quantity": 1,
            "entry": 1450.0,
            "stop": 1400.0,
            "target": 1550.0,
            "amount": 1450.0,
            "source": "unit test",
        }
        values.update(overrides)
        return TradeProposal(**values)

    def test_accepts_valid_trade_inside_user_limits(self):
        validate_proposal(self.proposal())

    def test_rejects_amount_above_1500(self):
        with self.assertRaises(HTTPException):
            validate_proposal(self.proposal(quantity=2, amount=2900.0))

    def test_rejects_amount_below_100(self):
        with self.assertRaises(HTTPException):
            validate_proposal(self.proposal(entry=50.0, stop=40.0, target=60.0, amount=50.0))

    def test_rejects_amount_that_does_not_match_quantity(self):
        with self.assertRaises(HTTPException):
            validate_proposal(self.proposal(amount=1200.0))

    def test_rejects_invalid_stop_entry_target_order(self):
        with self.assertRaises(HTTPException):
            validate_proposal(self.proposal(stop=1500.0))


if __name__ == "__main__":
    unittest.main()
